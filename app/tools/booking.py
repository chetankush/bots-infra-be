"""Appointment tools.

Behaviour is credential-driven, not environment-driven: a tenant with a stored
`google_calendar` credential gets real free/busy and a real event; a tenant without
one keeps the local-database behaviour. Either way the tool result says which
happened, so the model never announces a calendar it did not actually write to.

Mocks are deterministic so eval replays are reproducible and never touch a real system.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import httpx

from app.config.schema import AgentConfig, BookingCfg
from app.db.models import Appointment, Location
from app.db.session import session_scope
from app.integrations.google_calendar import (
    GoogleCalendarError,
    GoogleCreds,
    free_busy,
    insert_event,
)
from app.logging import get_logger
from app.services.credentials import load_credential
from app.services.reminders import plan_for_appointment
from app.services.scheduling import (
    candidate_starts,
    get_zone,
    parse_iso_aware,
    subtract_busy,
)
from app.tools.registry import ToolSpec, registry

log = get_logger("booking")

# Deliberately strict. The address is handed to Google as an invitee, and it arrives
# from LLM-generated tool arguments - i.e. ultimately from a stranger's chat message.
_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,24}$")


def _safe_email(raw: str) -> str:
    value = (raw or "").strip()
    return value if _EMAIL.match(value) else ""


def _cfg(ctx: dict) -> BookingCfg:
    try:
        return AgentConfig.model_validate(ctx.get("config") or {}).booking
    except Exception:
        return BookingCfg()


async def _tenant_zone(ctx: dict) -> str:
    location_id = ctx.get("location_id")
    if not location_id:
        return "UTC"
    async with session_scope() as session:
        location = await session.get(Location, location_id)
        if location and location.tenant_id == ctx.get("tenant_id"):
            return location.timezone or "UTC"
    return "UTC"


async def _creds(ctx: dict) -> GoogleCreds | None:
    async with session_scope() as session:
        payload = await load_credential(
            session, tenant_id=ctx["tenant_id"], provider="google_calendar"
        )
    return GoogleCreds.from_payload(payload) if payload else None


# --------------------------------------------------------------------------- search


async def _search_mock(*, args: dict, ctx: dict) -> dict:
    base = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
    slots = [
        (base + timedelta(days=d)).replace(hour=h).isoformat()
        for d in range(3)
        for h in (9, 13, 16)
    ]
    return {"ok": True, "slots": slots[:6], "source": "mock"}


async def _search_real(*, args: dict, ctx: dict) -> dict:
    cfg = _cfg(ctx)
    tz_name = await _tenant_zone(ctx)
    tz = get_zone(tz_name)
    now = datetime.now(UTC)

    # Over-fetch, subtract busy, THEN trim. Trimming first would show a tenant with a
    # full calendar as almost fully booked - the busy blocks would eat the whole page.
    overfetch = min(cfg.max_slots * 6, 150)
    candidates = candidate_starts(
        now=now,
        tz=tz,
        hours=cfg.hours,
        closed_dates=cfg.closed_dates,
        horizon_days=cfg.horizon_days,
        slot_minutes=cfg.slot_minutes,
        slot_step_minutes=cfg.slot_step_minutes,
        lead_time_minutes=cfg.lead_time_minutes,
        max_slots=overfetch,
    )
    if not candidates:
        return {"ok": True, "slots": [], "source": "hours", "note": "no open hours in range"}

    creds = await _creds(ctx)
    if creds is None:
        return {
            "ok": True,
            "slots": [c.isoformat() for c in candidates[: cfg.max_slots]],
            "source": "hours",
            "degraded": True,
            "note": "no calendar connected; these are opening hours, not confirmed availability",
        }

    try:
        busy = await free_busy(
            creds, start=candidates[0], end=candidates[-1] + timedelta(minutes=cfg.slot_minutes)
        )
        free = subtract_busy(
            candidates, busy, slot_minutes=cfg.slot_minutes, buffer_minutes=cfg.buffer_minutes
        )
        return {
            "ok": True,
            "slots": [c.isoformat() for c in free[: cfg.max_slots]],
            "source": "google",
            "timezone": tz_name,
        }
    except (GoogleCalendarError, httpx.HTTPError, OSError) as exc:
        # Network timeouts are the common failure, not API errors - catching only
        # GoogleCalendarError would let a timeout crash a visitor's turn.
        log.warning("freebusy_degraded", error=f"{type(exc).__name__}: {exc}"[:200])
        return {
            "ok": True,
            "slots": [c.isoformat() for c in candidates[: cfg.max_slots]],
            "source": "hours",
            "degraded": True,
            "note": "calendar unreachable; offer these as provisional and confirm with the team",
        }


# ----------------------------------------------------------------------------- book


async def _book_mock(*, args: dict, ctx: dict) -> dict:
    return {
        "ok": True,
        "confirmation": "MOCK-CONF-001",
        "starts_at": args.get("starts_at"),
        "service": args.get("service", ""),
        "source": "mock",
    }


async def _book_real(*, args: dict, ctx: dict) -> dict:
    starts_at = parse_iso_aware(str(args.get("starts_at", "")))
    if starts_at is None:
        return {"ok": False, "error": "starts_at must be an ISO-8601 datetime"}
    if starts_at < datetime.now(UTC) - timedelta(minutes=5):
        return {"ok": False, "error": "that time is in the past"}

    cfg = _cfg(ctx)
    tz_name = await _tenant_zone(ctx)
    ends_at = starts_at + timedelta(minutes=cfg.slot_minutes)

    name = str(args.get("name", ""))[:120]
    phone = str(args.get("phone", ""))[:40]
    service = str(args.get("service", ""))[:120]
    email = _safe_email(str(args.get("email", "")))

    external_ref, source = "", "local"
    creds = await _creds(ctx)

    if creds is not None:
        summary = cfg.event_summary.format(
            service=service or "Appointment", name=name or "Customer"
        )
        description = "\n".join(
            filter(
                None,
                [
                    f"Name: {name}",
                    f"Phone: {phone}",
                    f"Service: {service}",
                    f"Vehicle: {args.get('vehicle', '')}",
                    "Booked by the website assistant.",
                ],
            )
        )
        try:
            created = await insert_event(
                creds,
                start=starts_at,
                end=ends_at,
                summary=summary,
                description=description,
                timezone_name=tz_name,
                attendee_email=email,
            )
            external_ref, source = created["id"], "google"
        except (GoogleCalendarError, httpx.HTTPError, OSError) as exc:
            log.warning("calendar_insert_failed", error=f"{type(exc).__name__}: {exc}"[:200])
            source = "local_after_calendar_error"

    async with session_scope() as session:
        appointment = Appointment(
            tenant_id=ctx["tenant_id"],
            location_id=ctx.get("location_id"),
            conversation_id=ctx["conversation_id"],
            starts_at=starts_at,
            service=service,
            contact={"name": name, "phone": phone, "email": email},
            external_ref=external_ref,
        )
        session.add(appointment)
        await session.flush()
        appointment_id = appointment.id
        if not external_ref:
            external_ref = str(appointment.id)[:8].upper()
            appointment.external_ref = external_ref

    try:
        full = AgentConfig.model_validate(ctx.get("config") or {})
        await plan_for_appointment(
            tenant_id=ctx["tenant_id"],
            appointment_id=appointment_id,
            starts_at=starts_at,
            cfg=full.reminders,
            tz_name=tz_name,
        )
    except Exception as exc:
        # A reminder that fails to schedule must never fail the booking itself.
        log.warning("reminder_planning_failed", error=f"{type(exc).__name__}: {exc}"[:160])

    result = {
        "ok": True,
        "confirmation": external_ref,
        "starts_at": starts_at.isoformat(),
        "timezone": tz_name,
        "source": source,
    }
    if source != "google":
        # The model must not tell a customer "it's in our calendar" when it isn't.
        result["note"] = "recorded internally; the team will confirm the calendar entry"
    return result


registry.register(
    ToolSpec(
        name="search_availability",
        description=(
            "Look up open appointment slots. Call this before offering any time to the "
            "customer. Never invent availability. If the result says degraded, present "
            "the times as provisional rather than confirmed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "Type of visit, e.g. service, test drive",
                },
                "preferred_date": {
                    "type": "string",
                    "description": "ISO date the customer prefers",
                },
            },
            "required": [],
        },
        executor=_search_real,
        mock_executor=_search_mock,
        required_credentials=["google_calendar"],
    )
)

registry.register(
    ToolSpec(
        name="book_appointment",
        description=(
            "Book a confirmed appointment. Only call after you have the customer's name, "
            "a contact number, and a slot returned by search_availability."
        ),
        parameters={
            "type": "object",
            "properties": {
                "starts_at": {
                    "type": "string",
                    "description": "ISO-8601 start time from search_availability",
                },
                "service": {"type": "string"},
                "name": {"type": "string"},
                "phone": {"type": "string"},
                "email": {"type": "string"},
                "vehicle": {"type": "string"},
            },
            "required": ["starts_at", "name", "phone"],
        },
        executor=_book_real,
        mock_executor=_book_mock,
        required_credentials=["google_calendar"],
    )
)
