"""Background worker.

Runs in its own container with a CPU quota so crawling and embedding never starve
request serving. Postgres is the queue: SELECT ... FOR UPDATE SKIP LOCKED.

Handles two job kinds:
  crawl    - ingest a client's site
  webhook  - deliver an outbound event, with exponential backoff
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from app.db.models import JobQueue, Tenant
from app.db.session import session_scope
from app.logging import configure, get_logger
from app.rag.ingest import ingest_site
from app.services.email import deliver_email
from app.services.reminders import send_due
from app.services.retention import purge_tenant
from app.services.webhooks import MAX_ATTEMPTS, deliver

configure()
log = get_logger("worker")

CLAIM = text(
    """
    UPDATE job_queue SET status = 'running', attempts = attempts + 1
    WHERE id = (
        SELECT id FROM job_queue
        WHERE status = 'pending' AND run_after <= now()
        ORDER BY run_after
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING CAST(id AS text) AS id, kind, payload,
              CAST(tenant_id AS text) AS tenant_id, attempts
    """
)

FINISH = text("UPDATE job_queue SET status = :s, last_error = :e WHERE id = CAST(:i AS uuid)")
RETRY = text(
    """
    UPDATE job_queue SET status = 'pending', last_error = :e, run_after = :run_after
    WHERE id = CAST(:i AS uuid)
    """
)


def _backoff(attempts: int) -> timedelta:
    """1m, 4m, 9m, 16m - quadratic, so a dead endpoint stops being hammered."""
    return timedelta(minutes=min(attempts, 4) ** 2)


async def _run_crawl(job) -> None:
    payload = job["payload"] or {}
    async with session_scope() as session:
        await ingest_site(
            session,
            tenant_id=job["tenant_id"],
            start_url=payload["url"],
            max_pages=int(payload.get("max_pages", 40)),
        )


async def schedule_retention() -> int:
    """Enqueue one purge job per tenant that has a retention window.

    Uses the ORM rather than a raw INSERT: `job_queue.attempts` and `last_error` are
    NOT NULL with Python-side defaults, so a raw INSERT that omits them fails.
    Idempotent - a tenant already holding a pending purge job is skipped.
    """
    queued = 0
    async with session_scope() as session:
        tenants = (
            (
                await session.execute(
                    select(Tenant).where(Tenant.status != "paused", Tenant.retention_days > 0)
                )
            )
            .scalars()
            .all()
        )

        for tenant in tenants:
            pending = (
                await session.execute(
                    select(JobQueue.id).where(
                        JobQueue.tenant_id == tenant.id,
                        JobQueue.kind == "retention",
                        JobQueue.status.in_(("pending", "running")),
                    )
                )
            ).first()
            if pending:
                continue
            session.add(
                JobQueue(
                    tenant_id=tenant.id,
                    kind="retention",
                    payload={"retention_days": tenant.retention_days},
                )
            )
            queued += 1

    log.info("retention_scheduled", queued=queued)
    return queued


async def _run_retention(job) -> None:
    payload = job["payload"] or {}
    await purge_tenant(
        tenant_id=uuid.UUID(job["tenant_id"]),
        retention_days=int(payload.get("retention_days", 365)),
    )


async def _handle(job) -> tuple[str, str]:
    """Return (status, error). 'pending' means schedule a retry."""
    kind = job["kind"]

    if kind == "crawl":
        await _run_crawl(job)
        return "done", ""

    if kind == "retention":
        await _run_retention(job)
        return "done", ""

    if kind == "email":
        result = await deliver_email(job["payload"] or {})
        if result.get("ok"):
            return "done", ""
        if result.get("permanent"):
            return "failed", str(result.get("error") or result.get("status"))
        raise RuntimeError(f"email send failed with {result.get('status')}")

    if kind == "webhook":
        result = await deliver(job["payload"] or {})
        if result.get("ok"):
            return "done", ""
        if result.get("permanent"):
            # The receiver rejected it outright; retrying cannot help.
            return "failed", f"rejected with {result.get('status')}"
        raise RuntimeError(f"delivery failed with {result.get('status')}")

    return "failed", f"unknown job kind: {kind}"


RETENTION_INTERVAL = timedelta(hours=24)
REMINDER_INTERVAL = timedelta(minutes=5)


async def loop(poll_seconds: float = 3.0) -> None:
    log.info("worker_started")
    next_retention = datetime.now(UTC)
    next_reminders = datetime.now(UTC)

    while True:
        if datetime.now(UTC) >= next_retention:
            next_retention = datetime.now(UTC) + RETENTION_INTERVAL
            try:
                await schedule_retention()
            except Exception as exc:
                log.error("retention_schedule_failed", error=str(exc)[:200])

        if datetime.now(UTC) >= next_reminders:
            next_reminders = datetime.now(UTC) + REMINDER_INTERVAL
            try:
                await send_due()
            except Exception as exc:
                log.error("reminder_sweep_failed", error=str(exc)[:200])

        async with session_scope() as session:
            job = (await session.execute(CLAIM)).mappings().first()

        if not job:
            await asyncio.sleep(poll_seconds)
            continue

        try:
            status, error = await _handle(job)
        except Exception as exc:
            attempts = int(job["attempts"])
            error = f"{type(exc).__name__}: {exc}"[:500]
            if attempts >= MAX_ATTEMPTS:
                log.error("job_exhausted", job_id=job["id"], kind=job["kind"], error=error)
                status = "failed"
            else:
                run_after = datetime.now(UTC) + _backoff(attempts)
                log.warning(
                    "job_retry", job_id=job["id"], kind=job["kind"], attempt=attempts, error=error
                )
                async with session_scope() as session:
                    await session.execute(
                        RETRY, {"e": error, "i": job["id"], "run_after": run_after}
                    )
                continue

        async with session_scope() as session:
            await session.execute(FINISH, {"s": status, "e": error, "i": job["id"]})


if __name__ == "__main__":
    asyncio.run(loop())
