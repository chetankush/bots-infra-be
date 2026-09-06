"""Crawl -> chunk -> embed -> store, scoped to one tenant."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, Document
from app.logging import get_logger
from app.rag.chunker import chunk_text, content_hash
from app.rag.crawler import crawl
from app.rag.embed import embed

log = get_logger("ingest")


async def ingest_site(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    start_url: str,
    location_id: uuid.UUID | None = None,
    max_pages: int = 40,
) -> dict:
    pages = await crawl(start_url, max_pages=max_pages)
    documents = chunks_written = skipped = 0

    for page in pages:
        digest = content_hash(page.text)

        existing = (
            await session.execute(
                select(Document).where(Document.tenant_id == tenant_id, Document.url == page.url)
            )
        ).scalar_one_or_none()

        if existing and existing.content_hash == digest:
            skipped += 1
            continue

        if existing:
            await session.execute(delete(Chunk).where(Chunk.document_id == existing.id))
            existing.content_hash = digest
            existing.title = page.title
            document = existing
        else:
            document = Document(
                tenant_id=tenant_id,
                location_id=location_id,
                url=page.url,
                title=page.title,
                content_hash=digest,
            )
            session.add(document)
            await session.flush()

        pieces = chunk_text(page.text)
        if not pieces:
            continue

        vectors = await embed(pieces)
        for content, vector in zip(pieces, vectors, strict=True):
            session.add(
                Chunk(
                    tenant_id=tenant_id,
                    location_id=location_id,
                    document_id=document.id,
                    content=content,
                    source_url=page.url,
                    embedding=vector,
                )
            )
        documents += 1
        chunks_written += len(pieces)

    await session.commit()
    result = {
        "pages_crawled": len(pages),
        "documents_indexed": documents,
        "chunks": chunks_written,
        "unchanged": skipped,
    }
    log.info("ingest_done", tenant_id=str(tenant_id), **result)
    return result
