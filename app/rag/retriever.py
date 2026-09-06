"""Tenant- and location-scoped vector retrieval."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.schema import RetrievalCfg
from app.rag.embed import embed_one

# NOTE: postgres '::type' casts collide with SQLAlchemy's ':param' bind syntax when
# they touch a bind name. Use CAST(...) around every bound parameter.
_BASE_SQL = """
    SELECT CAST(id AS text) AS id, content, source_url,
           1 - (embedding <=> CAST(:vec AS vector)) AS score
    FROM chunks
    WHERE tenant_id = CAST(:tenant_id AS uuid)
      {location_clause}
    ORDER BY embedding <=> CAST(:vec AS vector)
    LIMIT :k
"""

_LOCATION_CLAUSE = "AND (location_id IS NULL OR location_id = CAST(:location_id AS uuid))"


async def retrieve(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    location_id: uuid.UUID | None,
    query: str,
    cfg: RetrievalCfg,
) -> list[dict]:
    """Cosine search over this tenant's chunks only.

    The tenant predicate is inside the SQL, not applied afterwards - filtering after an
    ANN search is how small tenants end up retrieving nothing.
    """
    if not query.strip():
        return []

    vector = await embed_one(query)
    literal = "[" + ",".join(f"{v:.6f}" for v in vector) + "]"

    params: dict[str, object] = {
        "vec": literal,
        "tenant_id": str(tenant_id),
        "k": cfg.top_k,
    }
    clause = ""
    if location_id is not None:
        clause = _LOCATION_CLAUSE
        params["location_id"] = str(location_id)

    rows = (
        (await session.execute(text(_BASE_SQL.format(location_clause=clause)), params))
        .mappings()
        .all()
    )

    hits = [dict(r) for r in rows if float(r["score"]) >= cfg.min_score]

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
