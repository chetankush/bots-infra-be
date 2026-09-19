"""Cross-encoder reranking via fastembed. Local ONNX, no PyTorch, same as embed.py.

Why a second model: the bi-encoder in embed.py scores the query and each chunk
*separately*, which is what makes it cheap enough to index a whole corpus, but it
never sees the two together. A cross-encoder reads (query, chunk) as one input and is
far better at judging relevance, at the cost of being too slow to run over everything.
So it runs only over the short candidate list retrieval has already produced.

The model is lazily loaded once per process and reused.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache

from app.settings import get_settings


@lru_cache(maxsize=1)
def _model():
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return TextCrossEncoder(model_name=get_settings().rerank_model)


def rerank_sync(query: str, documents: list[str]) -> list[float]:
    """Relevance score per document, same order as input. Higher is better."""
    if not documents:
        return []
    return [float(s) for s in _model().rerank(query, documents)]


async def rerank(query: str, documents: list[str]) -> list[float]:
    """Off-thread so a rerank pass never blocks the event loop."""
    return await asyncio.to_thread(rerank_sync, query, documents)


def warmup() -> None:
    _model().rerank("warmup", ["warmup"])
