"""Reciprocal Rank Fusion: merge ranked lists whose scores are not comparable.

Cosine similarity (dense) and ts_rank (sparse) live on different scales, so adding
them is meaningless. RRF ignores the scores and uses only each document's *position*
in each list:

    rrf(d) = sum over lists of  1 / (k + rank_in_list(d))

A document ranked well in both lists accumulates from both and wins. `k` damps the
advantage of being #1 in a single list; 60 is the value from Cormack, Clarke and
Buettcher (2009) and is rarely worth tuning.
"""

from __future__ import annotations


def rrf_fuse(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """Fuse ranked id lists into one list of (id, score), best first.

    Ties keep first-seen order, so a stable input gives a stable output.
    """
    if k <= 0:
        raise ValueError("k must be positive")

    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)

    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
