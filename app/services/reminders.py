"""Appointment reminders.

Unlike tokens, every send costs real money and reaches a real person, so the whole
design is defensive:

- Idempotency is a UNIQUE(appointment_id, rule_key) constraint. Two workers racing,
  a retry, or a repeated sweep can physically only produce one row, so "sent twice"
  is not a bug this code can have.
- A reminder is planned at booking time and claimed atomically at send time, so a
  crash between claim and send leaves it claimed rather than re-sendable.
- Attempts are capped and terminal failures stay terminal. A sweep that requeues
  anything still pending would resurrect permanently-failed rows forever.
- Cancellation is checked at send time against the live appointment, not at plan time.
- Quiet hours are applied in the location's timezone.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.config.schema import ReminderCfg
from app.db.models import Appointment, Location, Reminder, Tenant
from app.db.session import session_scope
from app.logging import get_logger
from app.services.email import appointment_body, enqueue_email
from app.services.scheduling import get_zone

log = get_logger("reminders")

SWEEP_LIMIT = 200  # per tick; a bound, not a target


def in_quiet_hours(when: datetime, quiet: list[str], tz_name: str) -> bool:
    """Quiet hours are evaluated in local time and may wrap midnight."""
    local = when.astimezone(get_zone(tz_name)).time()
    for start, end in parse_ranges_wrapping(quiet):
        if start <= end:
            if start <= local < end:
                return True
        elif local >= start or local < end:  # wraps midnight
            return True
    return False


def parse_ranges_wrapping(spec: list[str]) -> list[tuple[time, time]]:
    """Like parse_ranges but keeps inverted ranges - '21:00-08:00' is meaningful here."""
    out: list[tuple[time, time]] = []
    for item in spec or []:
        try:
            a, b = str(item).split("-", 1)
            ah, am = (int(x) for x in a.strip().split(":"))
            bh, bm = (int(x) for x in b.strip().split(":"))
            out.append((time(ah, am), time(bh, bm)))
        except (ValueError, TypeError):
            continue
    return out


def shift_out_of_quiet(when: datetime, quiet: list[str], tz_name: str) -> datetime:
    """Nudge forward in whole hours until outside quiet hours. Bounded at 24 tries."""
    candidate = when
    for _ in range(24):
        if not in_quiet_hours(candidate, quiet, tz_name):
            return candidate
        candidate += timedelta(hours=1)
    return when


async def plan_for_appointment(
    *,
    tenant_id: uuid.UUID,
    appointment_id: uuid.UUID,
    starts_at: datetime,
    cfg: ReminderCfg,
    tz_name: str = "UTC",
) -> int:
    """Create reminder rows. Safe to call repeatedly - the constraint dedupes."""
    if not cfg.enabled or not cfg.rules:
        return 0

    planned = 0
    now = datetime.now(UTC)

    for rule in cfg.rules[: cfg.max_per_appointment]:
        send_at = starts_at - timedelta(hours=rule.hours_before)
        if send_at <= now:
            continue  # booked inside the window; nothing to schedule
        send_at = shift_out_of_quiet(send_at, cfg.quiet_hours, tz_name)

        try:
            async with session_scope() as session:
                session.add(
                    Reminder(
                        tenant_id=tenant_id,
                        appointment_id=appointment_id,
                        rule_key=rule.key[:48],
                        send_at=send_at,
                        channel="email",
                    )
                )
            planned += 1
        except IntegrityError:
            # Already planned for this (appointment, rule). Exactly the intended outcome.
            continue

    if planned:
        log.info("reminders_planned", appointment_id=str(appointment_id), count=planned)
    return planned


async def cancel_for_appointment(*, appointment_id: uuid.UUID) -> int:
    async with session_scope() as session:
        result = await session.execute(
            text("""
            UPDATE reminders SET status = 'cancelled'
            WHERE appointment_id = CAST(:aid AS uuid) AND status = 'planned'
            """),
            {"aid": str(appointment_id)},
        )
        return result.rowcount or 0


# Claims one due reminder atomically. Terminal states are excluded, so a failed row is
# never picked up again - a sweep that requeues "anything pending" resurrects rows
# forever and bills the client for every resurrection.
_CLAIM = text("""
    UPDATE reminders SET status = 'sending', attempts = attempts + 1
    WHERE id = (
        SELECT id FROM reminders
        WHERE status = 'planned' AND send_at <= now()
        ORDER BY send_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING CAST(id AS text) AS id, CAST(tenant_id AS text) AS tenant_id,
              CAST(appointment_id AS text) AS appointment_id, rule_key, attempts
""")


async def _finish(reminder_id: str, status: str, error: str = "") -> None:
    async with session_scope() as session:
        await session.execute(
            # The same bind used as both a varchar assignment and a comparison operand
            # makes asyncpg fail type deduction (AmbiguousParameterError), so the status
            # is passed twice under distinct names.
            text("""
            UPDATE reminders
            SET status = :s, last_error = :e,
                sent_at = CASE WHEN :s_check = 'sent' THEN now() ELSE sent_at END
            WHERE id = CAST(:i AS uuid)
            """),
            {"s": status, "s_check": status, "e": error[:400], "i": reminder_id},
        )


async def send_due(limit: int = SWEEP_LIMIT) -> dict:
    """Send every due reminder, up to `limit`. Returns a per-outcome tally."""
    tally = {"sent": 0, "cancelled": 0, "failed": 0, "skipped": 0}

    for _ in range(limit):
        async with session_scope() as session:
            claimed = (await session.execute(_CLAIM)).mappings().first()
        if not claimed:
            break

        rid = claimed["id"]
        try:
            async with session_scope() as session:
                appointment = await session.get(Appointment, uuid.UUID(claimed["appointment_id"]))
                tenant = await session.get(Tenant, uuid.UUID(claimed["tenant_id"]))

                # Cancellation is decided here, against the live row - an appointment
                # cancelled after planning must not still get a reminder.
                if appointment is None or appointment.status != "booked":
                    await _finish(rid, "cancelled", "appointment no longer booked")
                    tally["cancelled"] += 1
                    continue

                if appointment.starts_at <= datetime.now(UTC):
                    await _finish(rid, "cancelled", "appointment already started")
                    tally["cancelled"] += 1
                    continue

                location = (
                    await session.get(Location, appointment.location_id)
                    if appointment.location_id
                    else None
                )
                tz_name = location.timezone if location else "UTC"
                contact = appointment.contact or {}
                business = tenant.name if tenant else "your appointment"
                local_when = appointment.starts_at.astimezone(get_zone(tz_name))
                to = [contact.get("email", "")]

            queued = await enqueue_email(
                tenant_id=uuid.UUID(claimed["tenant_id"]),
                to=to,
                subject=f"Reminder: your appointment with {business}",
                text=appointment_body(
                    business=business,
                    starts_at=local_when.strftime("%A %d %B, %H:%M (%Z)"),
                    service=appointment.service,
                    confirmed=True,
                ),
                kind="reminder",
            )
            if queued:
                await _finish(rid, "sent")
                tally["sent"] += 1
            else:
                # No contactable address - terminal, not retryable.
                await _finish(rid, "failed", "no valid recipient")
                tally["skipped"] += 1

        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if int(claimed["attempts"]) >= 3:
                await _finish(rid, "failed", error)
                tally["failed"] += 1
            else:
                async with session_scope() as session:
                    await session.execute(
                        text(
                            "UPDATE reminders SET status='planned', last_error=:e, "
                            "send_at = now() + interval '10 minutes' "
                            "WHERE id = CAST(:i AS uuid)"
                        ),
                        {"e": error[:400], "i": rid},
                    )
            log.warning("reminder_error", reminder_id=rid, error=error[:200])

    if any(tally.values()):
        log.info("reminders_swept", **tally)
    return tally


async def pending_count(tenant_id: uuid.UUID) -> int:
    async with session_scope() as session:
        return (
            await session.execute(
                select(Reminder).where(
                    Reminder.tenant_id == tenant_id, Reminder.status == "planned"
                )
            )
        ).raw.rowcount or 0
