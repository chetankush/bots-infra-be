"""Business-hours slot maths.

Pure: no I/O, no network, and the clock is always passed in. That makes every DST and
boundary case a plain unit test rather than something you find out about in production.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAY_KEYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def get_zone(name: str) -> ZoneInfo:
    """A bad tenant timezone must never raise mid-conversation."""
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def parse_ranges(spec: list[str]) -> list[tuple[time, time]]:
    """['09:00-13:00', '14:00-18:00'] -> [(09:00, 13:00), (14:00, 18:00)].

    Config is operator-authored, so malformed and inverted ranges are dropped rather
    than raising.
    """
    out: list[tuple[time, time]] = []
    for item in spec or []:
        try:
            start_s, end_s = str(item).split("-", 1)
            sh, sm = (int(x) for x in start_s.strip().split(":"))
            eh, em = (int(x) for x in end_s.strip().split(":"))
            start, end = time(sh, sm), time(eh, em)
        except (ValueError, TypeError):
            continue
        if start < end:
            out.append((start, end))
    return out


def day_windows(
    day: date, hours: dict[str, list[str]], tz: ZoneInfo
) -> list[tuple[datetime, datetime]]:
    """Aware local open windows for one calendar day."""
    ranges = parse_ranges((hours or {}).get(WEEKDAY_KEYS[day.weekday()], []))
    return [
        (
            datetime.combine(day, start, tzinfo=tz),
            datetime.combine(day, end, tzinfo=tz),
        )
        for start, end in ranges
    ]


def candidate_starts(
    *,
    now: datetime,
    tz: ZoneInfo,
    hours: dict[str, list[str]],
    closed_dates: list[str],
    horizon_days: int,
    slot_minutes: int,
    slot_step_minutes: int,
    lead_time_minutes: int,
    max_slots: int,
) -> list[datetime]:
    """Open slot starts, ascending. Bounded by horizon_days and max_slots."""
    closed = set(closed_dates or [])
    earliest = now + timedelta(minutes=lead_time_minutes)
    step = timedelta(minutes=max(slot_step_minutes, 5))
    length = timedelta(minutes=max(slot_minutes, 5))

    out: list[datetime] = []
    local_today = now.astimezone(tz).date()

    for offset in range(max(horizon_days, 0)):
        day = local_today + timedelta(days=offset)
        if day.isoformat() in closed:
            continue
        for window_start, window_end in day_windows(day, hours, tz):
            cursor = window_start
            while cursor + length <= window_end:
                if cursor >= earliest:
                    out.append(cursor)
                    if len(out) >= max_slots:
                        return out
                cursor += step
    return out


def overlaps(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> bool:
    return a0 < b1 and b0 < a1


def subtract_busy(
    candidates: list[datetime],
    busy: list[tuple[datetime, datetime]],
    *,
    slot_minutes: int,
    buffer_minutes: int = 0,
) -> list[datetime]:
    """Drop candidates whose slot (plus buffer) collides with a busy block."""
    if not busy:
        return candidates
    length = timedelta(minutes=slot_minutes)
    pad = timedelta(minutes=buffer_minutes)
    return [
        c
        for c in candidates
        if not any(overlaps(c - pad, c + length + pad, b0, b1) for b0, b1 in busy)
    ]


def parse_iso_aware(raw: str) -> datetime | None:
    """Tolerant ISO-8601 parse. Naive input is assumed UTC; junk returns None."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def to_rfc3339(dt: datetime) -> str:
    """UTC, Z-suffixed - the shape Google's freeBusy wants."""
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
