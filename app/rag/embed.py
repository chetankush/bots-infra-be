"""Local ONNX embeddings via fastembed - no PyTorch, ARM-friendly, $0 per token.

The model is lazily loaded once per process and reused.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache

from app.settings import get_settings


@lru_cache(maxsize=1)
def _model():
    from fastembed import TextEmbedding

    s = get_settings()
    return TextEmbedding(model_name=s.embedding_model)


def embed_sync(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    return [vec.tolist() for vec in _model().embed(texts)]


async def embed(texts: list[str]) -> list[list[float]]:
    """Off-thread so embedding never blocks the event loop."""
    return await asyncio.to_thread(embed_sync, texts)


async def embed_one(text: str) -> list[float]:
    out = await embed([text])
    return out[0]


def warmup() -> None:
    _model().embed(["warmup"])
