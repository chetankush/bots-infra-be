"""Tenant- and location-scoped hybrid retrieval: dense + sparse, fused, reranked.

Pipeline per query:

    dense   : pgvector cosine over this tenant's chunks   -> top `candidates`
    sparse  : Postgres full-text (tsvector/GIN)            -> top `candidates`
    fuse    : Reciprocal Rank Fusion of the two id lists
    rerank  : cross-encoder scores the fused top `candidates`
    cut     : take `top_k`, then trim to `max_context_chars`

Each leg carries the tenant predicate *inside* the SQL. Filtering after an ANN
search is how small tenants end up retrieving nothing, and filtering after a
sparse search is how one tenant reads another's documents.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.schema import RetrievalCfg
from app.rag.embed import embed_one
from app.rag.fusion import rrf_fuse
from app.rag.rerank import rerank

# NOTE: postgres '::type' casts collide with SQLAlchemy's ':param' bind syntax when
# they touch a bind name. Use CAST(...) around every bound parameter.
_DENSE_SQL = """
    SELECT CAST(id AS text) AS id, content, source_url,
           1 - (embedding <=> CAST(:vec AS vector)) AS score
    FROM chunks
    WHERE tenant_id = CAST(:tenant_id AS uuid)
      {location_clause}
    ORDER BY embedding <=> CAST(:vec AS vector)
    LIMIT :k
"""

# websearch_to_tsquery accepts free text ("brake pads 2019 corolla") without ever
# raising on user punctuation, unlike to_tsquery which wants operator syntax.
_SPARSE_SQL = """
    SELECT CAST(id AS text) AS id, content, source_url,
           ts_rank_cd(tsv, websearch_to_tsquery('english', :q)) AS score
    FROM chunks
    WHERE tenant_id = CAST(:tenant_id AS uuid)
      {location_clause}
      AND tsv @@ websearch_to_tsquery('english', :q)
    ORDER BY score DESC
    LIMIT :k
"""

_LOCATION_CLAUSE = "AND (location_id IS NULL OR location_id = CAST(:location_id AS uuid))"


async def _run(session: AsyncSession, sql: str, params: dict) -> list[dict]:
    rows = (await session.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


async def retrieve(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    location_id: uuid.UUID | None,
    query: str,
    cfg: RetrievalCfg,
) -> list[dict]:
    """Return the best chunks for `query` within this tenant, best first.

    Each hit is {id, content, source_url, score, via} where `via` records which
    legs found it ("dense", "sparse", or "both"), which is what you want in a trace
    when a retrieval looks wrong.
    """
    if not query.strip():
        return []

    clause = ""
    scope: dict[str, object] = {"tenant_id": str(tenant_id)}
    if location_id is not None:
        clause = _LOCATION_CLAUSE
        scope["location_id"] = str(location_id)

    n = cfg.candidates if cfg.hybrid else cfg.top_k

    # Dense leg. min_score applies here: it is a cosine threshold and means nothing
    # for the sparse leg or for RRF scores.
    vector = await embed_one(query)
    literal = "[" + ",".join(f"{v:.6f}" for v in vector) + "]"
    dense = await _run(
        session, _DENSE_SQL.format(location_clause=clause), {**scope, "vec": literal, "k": n}
    )
    dense = [h for h in dense if float(h["score"]) >= cfg.min_score]

    if not cfg.hybrid:
        return _budget(dense, cfg)

    # Sparse leg. No threshold: a keyword match is a keyword match.
    sparse = await _run(
        session, _SPARSE_SQL.format(location_clause=clause), {**scope, "q": query, "k": n}
    )

    # Fuse by rank, not score.
    by_id: dict[str, dict] = {}
    for h in dense:
        by_id[h["id"]] = {**h, "via": "dense"}
    for h in sparse:
        if h["id"] in by_id:
            by_id[h["id"]]["via"] = "both"
        else:
            by_id[h["id"]] = {**h, "via": "sparse"}

    fused = rrf_fuse([[h["id"] for h in dense], [h["id"] for h in sparse]], k=cfg.rrf_k)
    candidates = [dict(by_id[doc_id], score=score) for doc_id, score in fused[: cfg.candidates]]

    if cfg.rerank and candidates:
        scores = await rerank(query, [c["content"] for c in candidates])
        for c, s in zip(candidates, scores, strict=True):
            c["score"] = s
        candidates.sort(key=lambda c: c["score"], reverse=True)

    return _budget(candidates[: cfg.top_k], cfg)


def _budget(hits: list[dict], cfg: RetrievalCfg) -> list[dict]:
    """Keep hits in order until the context character budget is spent."""
    out, budget = [], cfg.max_context_chars
    for hit in hits:
        content = hit["content"]
        if budget - len(content) < 0:
            break
        budget -= len(content)
        out.append(hit)
    return out


def format_context(hits: list[dict]) -> str:
    if not hits:
        return "(no matching information found in the business's published content)"
    return "\n\n".join(
        f"[source {i + 1}: {h.get('source_url') or 'site'}]\n{h['content']}"
        for i, h in enumerate(hits)
    )
