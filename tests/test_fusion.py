"""RRF is the piece that decides which chunk the model sees first. Get it wrong quietly
and every answer is subtly worse with no error to point at."""

import pytest

from app.rag.fusion import rrf_fuse


def test_document_in_both_lists_beats_document_in_one():
    dense = ["a", "b", "c"]
    sparse = ["b", "d", "e"]
    fused = rrf_fuse([dense, sparse], k=60)
    assert fused[0][0] == "b"  # rank 2 + rank 1 > rank 1 alone


def test_only_rank_matters_not_list_length():
    # "a" is #1 in a long list; "z" is #1 in a short one. Same contribution.
    fused = dict(rrf_fuse([["a", "b", "c", "d"], ["z"]], k=60))
    assert fused["a"] == pytest.approx(fused["z"])


def test_scores_are_one_over_k_plus_rank():
    fused = dict(rrf_fuse([["a", "b"]], k=60))
    assert fused["a"] == pytest.approx(1 / 61)
    assert fused["b"] == pytest.approx(1 / 62)


def test_larger_k_flattens_the_gap_between_ranks():
    # k damps how much rank 1 beats rank 2. Small k: a big gap. Large k: nearly flat,
    # which is what lets a chunk found by both legs overtake a chunk that was #1 in one.
    small = dict(rrf_fuse([["a", "b"]], k=1))
    large = dict(rrf_fuse([["a", "b"]], k=1000))
    assert small["a"] / small["b"] > 1.4
    assert large["a"] / large["b"] < 1.01


def test_empty_and_single_list():
    assert rrf_fuse([]) == []
    assert rrf_fuse([[], []]) == []
    assert [d for d, _ in rrf_fuse([["a", "b"]])] == ["a", "b"]


def test_k_must_be_positive():
    with pytest.raises(ValueError):
        rrf_fuse([["a"]], k=0)
