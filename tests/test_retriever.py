"""The hybrid pipeline with Postgres, the embedder and the reranker all faked.

What these protect: the tenant predicate is in BOTH SQL legs, the dense threshold
does not leak onto the sparse leg, fusion prefers chunks found by both legs, the
reranker gets the final say, and the character budget still caps the output.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from app.config.schema import RetrievalCfg
from app.rag import retriever

TENANT = uuid.uuid4()


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class FakeSession:
    """Routes the dense and sparse statements to canned rows and records the SQL."""

    def __init__(self, dense_rows, sparse_rows):
        self.dense_rows, self.sparse_rows = dense_rows, sparse_rows
        self.executed: list[tuple[str, dict]] = []

    async def execute(self, stmt, params):
        sql = str(stmt)
        self.executed.append((sql, params))
        return FakeResult(self.sparse_rows if "tsv" in sql else self.dense_rows)


def row(id_, content, score):
    return {"id": id_, "content": content, "source_url": "", "score": score}


@pytest.fixture
def fake_models(monkeypatch):
    monkeypatch.setattr(retriever, "embed_one", AsyncMock(return_value=[0.1, 0.2, 0.3]))
    # Reranker: score by content length so tests can predict the order deterministically.
    monkeypatch.setattr(
        retriever, "rerank", AsyncMock(side_effect=lambda q, docs: [float(len(d)) for d in docs])
    )


async def test_tenant_predicate_is_inside_both_legs(fake_models):
    s = FakeSession([row("a", "x", 0.9)], [row("a", "x", 1.0)])
    await retriever.retrieve(
        s, tenant_id=TENANT, location_id=None, query="brake pads", cfg=RetrievalCfg()
    )
    assert len(s.executed) == 2
    for sql, params in s.executed:
        assert "tenant_id = CAST(:tenant_id AS uuid)" in sql
        assert params["tenant_id"] == str(TENANT)


async def test_location_clause_applied_to_both_legs(fake_models):
    loc = uuid.uuid4()
    s = FakeSession([], [])
    await retriever.retrieve(s, tenant_id=TENANT, location_id=loc, query="q", cfg=RetrievalCfg())
    for sql, params in s.executed:
        assert "location_id" in sql and params["location_id"] == str(loc)


async def test_chunk_found_by_both_legs_is_marked_both(fake_models):
    dense = [row("a", "aaaa", 0.9), row("b", "bb", 0.8)]
    sparse = [row("b", "bb", 2.0), row("c", "c", 1.0)]
    s = FakeSession(dense, sparse)
    hits = await retriever.retrieve(
        s, tenant_id=TENANT, location_id=None, query="q", cfg=RetrievalCfg(rerank=False)
    )
    via = {h["id"]: h["via"] for h in hits}
    assert via == {"a": "dense", "b": "both", "c": "sparse"}
    assert hits[0]["id"] == "b"  # in both lists, so RRF ranks it first


async def test_min_score_filters_dense_only(fake_models):
    dense = [row("weak", "w", 0.10)]  # below default min_score 0.25
    sparse = [row("kw", "keyword hit", 0.5)]
    s = FakeSession(dense, sparse)
    hits = await retriever.retrieve(
        s, tenant_id=TENANT, location_id=None, query="q", cfg=RetrievalCfg(rerank=False)
    )
    assert [h["id"] for h in hits] == ["kw"]


async def test_reranker_reorders_fused_candidates(fake_models):
    # RRF would rank "a" first (in both lists). The fake reranker scores by length,
    # so the long chunk "c" must come out on top after reranking.
    dense = [row("a", "aa", 0.9), row("c", "c" * 50, 0.5)]
    sparse = [row("a", "aa", 1.0)]
    s = FakeSession(dense, sparse)
    hits = await retriever.retrieve(
        s, tenant_id=TENANT, location_id=None, query="q", cfg=RetrievalCfg()
    )
    assert hits[0]["id"] == "c"
    assert hits[0]["score"] == 50.0  # rerank score replaces the RRF score


async def test_top_k_and_char_budget_still_apply(fake_models):
    dense = [row(str(i), "x" * 100, 0.9) for i in range(10)]
    s = FakeSession(dense, [])
    cfg = RetrievalCfg(top_k=5, max_context_chars=250, rerank=False)
    hits = await retriever.retrieve(s, tenant_id=TENANT, location_id=None, query="q", cfg=cfg)
    assert len(hits) == 2  # 250 chars fits two 100-char chunks, not the 5 top_k allows


async def test_hybrid_off_is_dense_only(fake_models):
    s = FakeSession([row("a", "x", 0.9)], [row("b", "y", 1.0)])
    hits = await retriever.retrieve(
        s, tenant_id=TENANT, location_id=None, query="q", cfg=RetrievalCfg(hybrid=False)
    )
    assert len(s.executed) == 1 and "tsv" not in s.executed[0][0]
    assert [h["id"] for h in hits] == ["a"]


async def test_blank_query_hits_nothing(fake_models):
    s = FakeSession([row("a", "x", 0.9)], [])
    assert (
        await retriever.retrieve(
            s, tenant_id=TENANT, location_id=None, query="   ", cfg=RetrievalCfg()
        )
        == []
    )
    assert s.executed == []
