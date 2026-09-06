"""Sentence-aware chunking with overlap."""

from __future__ import annotations

import hashlib
import re

_SENT = re.compile(r"(?<=[.!?])\s+|\n{2,}")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


def chunk_text(text: str, *, target_chars: int = 900, overlap_chars: int = 150) -> list[str]:
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    if not text:
        return []
    if len(text) <= target_chars:
        return [text]

    pieces = [p.strip() for p in _SENT.split(text) if p and p.strip()]
    chunks: list[str] = []
    current = ""

    for piece in pieces:
        if len(current) + len(piece) + 1 <= target_chars:
            current = f"{current} {piece}".strip()
            continue
        if current:
            chunks.append(current)
        if len(piece) > target_chars:
            for i in range(0, len(piece), target_chars - overlap_chars):
                chunks.append(piece[i : i + target_chars])
            current = ""
        else:
            tail = current[-overlap_chars:] if current else ""
            current = f"{tail} {piece}".strip()

    if current:
        chunks.append(current)
    return [c for c in chunks if len(c) > 40]
