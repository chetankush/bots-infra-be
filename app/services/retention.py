"""Data retention: purge, erasure, and export.

The public site advertises data-retention compliance for the US, Europe and the UAE.
`Tenant.retention_days` existed as a column that nothing ever acted on, which meant the
promise was unbacked.

Three operations:
  purge_tenant  - routine: delete transcripts past the tenant's retention window
  erase_tenant  - erasure request: delete everything for one tenant
  export_tenant - portability request: everything we hold, minus other people's secrets

Design constraints these were built against, each from a real failure mode:
- Business records survive. Leads and appointments are SET NULL, not CASCADE, so a
  routine purge never destroys the client's pipeline (verified in test_retention.py).
- Every loop is bounded. A purge with no batch ceiling is an unbounded operation on a
  table that only ever grows.
- Batches commit independently and re-apply their own statement timeout - a timeout set
  once at the top is discarded by the first commit.
- Export redacts secrets. A tenant's own export must not hand back webhook signing keys
  or integration ciphertext.
- Dry run first. Deletion is irreversible; the only recovery is a nightly S3 dump.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import session_scope
from app.logging import get_logger

log = get_logger("retention")

BATCH_SIZE = 500
MAX_BATCHES = 200  # ceiling: 100k conversations per run, then the next run continues
BATCH_TIMEOUT_MS = 30_000

# Keys whose values must never appear in an export, at any nesting depth.
SECRET_KEYS = frozenset(
    {
        "webhook_secret",
        "lead_webhook_secret",
        "secret",
        "api_key",
        "client_secret",
        "refresh_token",
        "access_token",
        "auth_token",
        "password",
        "ciphertext",
    }
)


def redact(value: Any) -> Any:
    """Recursively blank anything that looks like a credential."""
    if isinstance(value, dict):
        return {
            k: ("[redacted]" if k.lower() in SECRET_KEYS else redact(v)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


async def _count_expiring(session: AsyncSession, *, tenant_id: uuid.UUID, cutoff: datetime) -> dict:
    row = (
        (
            await session.execute(
                text("""
            SELECT
              (SELECT count(*) FROM conversations
                 WHERE tenant_id = CAST(:tid AS uuid) AND created_at < :cutoff) AS conversations,
              (SELECT count(*) FROM messages m JOIN conversations c ON c.id = m.conversation_id
                 WHERE c.tenant_id = CAST(:tid AS uuid) AND c.created_at < :cutoff) AS messages,
              (SELECT count(*) FROM leads
                 WHERE tenant_id = CAST(:tid AS uuid) AND conversation_id IN
                   (SELECT id FROM conversations
                     WHERE tenant_id = CAST(:tid AS uuid) AND created_at < :cutoff)) AS leads_detached
            """),
                {"tid": str(tenant_id), "cutoff": cutoff},
            )
        )
        .mappings()
        .one()
    )
    return dict(row)


async def purge_tenant(
    *,
    tenant_id: uuid.UUID,
    retention_days: int,
    dry_run: bool = False,
    max_batches: int = MAX_BATCHES,
) -> dict:
    """Delete conversations (and cascading messages) past the retention window.

    Idempotent: re-running deletes only what is still past the cutoff. Safe to
    interrupt - each batch is its own transaction.
    """
    if retention_days <= 0:
        return {"skipped": "retention_days <= 0 means keep forever", "deleted": 0}

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)

    async with session_scope() as session:
        preview = await _count_expiring(session, tenant_id=tenant_id, cutoff=cutoff)

    if dry_run:
        return {
            "dry_run": True,
            "cutoff": cutoff.isoformat(),
            "would_delete": preview,
            "note": "leads and appointments are detached, not deleted",
        }

    deleted = batches = 0
    for _ in range(max_batches):
        async with session_scope() as session:
            # Re-applied per batch: SET LOCAL is scoped to the transaction, so a
            # timeout set once before the first commit protects only that batch.
            await session.execute(text(f"SET LOCAL statement_timeout = {BATCH_TIMEOUT_MS}"))
            result = await session.execute(
                text("""
                DELETE FROM conversations
                WHERE id IN (
                    SELECT id FROM conversations
                    WHERE tenant_id = CAST(:tid AS uuid) AND created_at < :cutoff
                    ORDER BY created_at
                    LIMIT :lim
                )
                """),
                {"tid": str(tenant_id), "cutoff": cutoff, "lim": BATCH_SIZE},
            )
            removed = result.rowcount or 0

        deleted += removed
        batches += 1
        if removed < BATCH_SIZE:
            break
    else:
        log.warning(
            "purge_hit_batch_ceiling",
            tenant_id=str(tenant_id),
            deleted=deleted,
            note="more remains; the next scheduled run continues",
        )

    log.info("purge_done", tenant_id=str(tenant_id), deleted=deleted, batches=batches)
    return {
        "cutoff": cutoff.isoformat(),
        "conversations_deleted": deleted,
        "batches": batches,
        "leads_detached": preview.get("leads_detached", 0),
        "complete": deleted < max_batches * BATCH_SIZE,
    }


async def erase_tenant(*, tenant_id: uuid.UUID, max_batches: int = MAX_BATCHES) -> dict:
    """Erasure request: remove everything for one tenant.

    Ordered child-first so no statement ever depends on a cascade firing correctly,
    and bounded per table so a large tenant cannot produce an unbounded run.
    """
    tables = [
        ("messages", "tenant_id"),
        ("leads", "tenant_id"),
        ("appointments", "tenant_id"),
        ("conversations", "tenant_id"),
        ("chunks", "tenant_id"),
        ("documents", "tenant_id"),
        ("eval_runs", "tenant_id"),
        ("eval_datasets", "tenant_id"),
        ("tenant_credentials", "tenant_id"),
        ("job_queue", "tenant_id"),
        ("locations", "tenant_id"),
    ]
    removed: dict[str, int] = {}
    truncated: list[str] = []

    for table, column in tables:
        total = 0
        for _ in range(max_batches):
            async with session_scope() as session:
                await session.execute(text(f"SET LOCAL statement_timeout = {BATCH_TIMEOUT_MS}"))
                result = await session.execute(
                    text(f"""
                    DELETE FROM {table}
                    WHERE ctid IN (
                        SELECT ctid FROM {table}
                        WHERE {column} = CAST(:tid AS uuid)
                        LIMIT :lim
                    )
                    """),
                    {"tid": str(tenant_id), "lim": BATCH_SIZE},
                )
                n = result.rowcount or 0
            total += n
            if n < BATCH_SIZE:
                break
        else:
            truncated.append(table)
        removed[table] = total

    async with session_scope() as session:
        await session.execute(
            text("DELETE FROM tenants WHERE id = CAST(:tid AS uuid)"),
            {"tid": str(tenant_id)},
        )

    log.info("erase_done", tenant_id=str(tenant_id), **{f"n_{k}": v for k, v in removed.items()})
    return {
        "erased": removed,
        "complete": not truncated,
        "truncated_tables": truncated,
    }


# Tables to include in an export, with the column used for stable ordering.
# `locations` and `chunks` have no timestamp column - ordering by id keeps the
# query valid instead of failing on a column that does not exist.
EXPORT_TABLES = {
    "locations": "id",
    "conversations": "created_at",
    "messages": "created_at",
    "leads": "created_at",
    "appointments": "created_at",
    "documents": "fetched_at",
}
EXPORT_ROW_CAP = 50_000


async def export_tenant(*, tenant_id: uuid.UUID) -> dict:
    """Portability request: everything held for one tenant, secrets redacted.

    Excluded by design: `tenant_credentials` (integration secrets belong to the agency,
    not the data subject) and `chunks.embedding` (derived, enormous, meaningless).
    """
    out: dict[str, Any] = {"tenant_id": str(tenant_id), "tables": {}}

    async with session_scope() as session:
        tenant = (
            (
                await session.execute(
                    text("SELECT * FROM tenants WHERE id = CAST(:tid AS uuid)"),
                    {"tid": str(tenant_id)},
                )
            )
            .mappings()
            .first()
        )
        out["tenant"] = redact({k: str(v) for k, v in dict(tenant or {}).items()})

        for table, order_col in EXPORT_TABLES.items():
            rows = (
                (
                    await session.execute(
                        text(f"""
                    SELECT * FROM {table}
                    WHERE tenant_id = CAST(:tid AS uuid)
                    ORDER BY {order_col}
                    LIMIT :cap
                    """),
                        {"tid": str(tenant_id), "cap": EXPORT_ROW_CAP},
                    )
                )
                .mappings()
                .all()
            )
            out["tables"][table] = [
                redact({k: (str(v) if v is not None else None) for k, v in dict(r).items()})
                for r in rows
            ]
            if len(rows) >= EXPORT_ROW_CAP:
                out.setdefault("truncated", []).append(table)

    out["excluded"] = {
        "tenant_credentials": "agency integration secrets, not subject data",
        "chunks": "derived embeddings of already-public site content",
    }
    return out
