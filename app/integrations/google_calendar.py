"""Google Calendar REST v3 over httpx.

No vendor SDK: the refresh-token flow plus two endpoints is a few dozen lines, and
google-api-python-client would drag a large dependency tree onto a free ARM box.

The base URLs are module-level and overridable so the whole client can be exercised
against a local fake server - no Google account is needed to test any of this.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import httpx

from app.logging import get_logger
from app.services.scheduling import parse_iso_aware, to_rfc3339

log = get_logger("gcal")

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/calendar/v3"

TIMEOUT = httpx.Timeout(8.0, connect=4.0)  # a visitor is waiting; never hang a turn
TOKEN_SKEW_SECONDS = 60
MAX_TOKEN_CACHE = 512


class GoogleCalendarError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, permanent: bool = False):
        super().__init__(message)
        self.status = status
        self.permanent = permanent


@dataclass(frozen=True)
class GoogleCreds:
    client_id: str
    client_secret: str
    refresh_token: str
    calendar_id: str = "primary"

    @classmethod
    def from_payload(cls, payload: dict) -> GoogleCreds | None:
        try:
            return cls(
                client_id=payload["client_id"],
                client_secret=payload["client_secret"],
                refresh_token=payload["refresh_token"],
                calendar_id=payload.get("calendar_id") or "primary",
            )
        except (KeyError, TypeError):
            return None

    @property
    def cache_key(self) -> str:
        """Keyed on the refresh token, not the tenant.

        Keying on tenant id alone would serve a stale token after a credential
        rotation; keying on the token itself makes rotation self-invalidating.
        """
        return f"{self.client_id}:{hash(self.refresh_token)}"


# key -> (access_token, expires_at_epoch)
_TOKENS: dict[str, tuple[str, float]] = {}


def _cache_get(key: str) -> str | None:
    hit = _TOKENS.get(key)
    if not hit:
        return None
    token, expires_at = hit
    if time.time() >= expires_at - TOKEN_SKEW_SECONDS:
        _TOKENS.pop(key, None)
        return None
    return token


def _cache_put(key: str, token: str, expires_in: float) -> None:
    if len(_TOKENS) >= MAX_TOKEN_CACHE:
        _TOKENS.clear()  # crude, bounded, and correct: tokens are cheap to re-fetch
    _TOKENS[key] = (token, time.time() + float(expires_in or 3600))


async def access_token(creds: GoogleCreds, *, client: httpx.AsyncClient | None = None) -> str:
    cached = _cache_get(creds.cache_key)
    if cached:
        return cached

    payload = {
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "refresh_token": creds.refresh_token,
        "grant_type": "refresh_token",
    }

    async def _post(c: httpx.AsyncClient):
        return await c.post(TOKEN_URL, data=payload)

    if client is not None:
        response = await _post(client)
    else:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            response = await _post(c)

    if response.status_code >= 400:
        # 400 invalid_grant means the user revoked access - retrying cannot fix it.
        permanent = response.status_code == 400
        raise GoogleCalendarError(
            f"token refresh failed ({response.status_code})",
            status=response.status_code,
            permanent=permanent,
        )

    body = response.json()
    token = body.get("access_token")
    if not token:
        raise GoogleCalendarError("token refresh returned no access_token", permanent=True)

    _cache_put(creds.cache_key, token, body.get("expires_in", 3600))
    return token


async def free_busy(
    creds: GoogleCreds,
    *,
    start,
    end,
    client: httpx.AsyncClient | None = None,
) -> list[tuple]:
    """Busy intervals for the tenant's calendar.

    Google returns HTTP 200 with a per-calendar `errors` array when the calendar id is
    wrong or access was revoked. Treating that as "no busy periods" would silently
    double-book every slot, so it is raised instead.
    """
    token = await access_token(creds, client=client)
    body = {
        "timeMin": to_rfc3339(start),
        "timeMax": to_rfc3339(end),
        "items": [{"id": creds.calendar_id}],
    }
    headers = {"Authorization": f"Bearer {token}"}

    async def _post(c: httpx.AsyncClient):
        return await c.post(f"{API_BASE}/freeBusy", json=body, headers=headers)

    if client is not None:
        response = await _post(client)
    else:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            response = await _post(c)

    if response.status_code >= 400:
        raise GoogleCalendarError(
            f"freeBusy failed ({response.status_code})",
            status=response.status_code,
            permanent=response.status_code in (401, 403, 404),
        )

    calendars = (response.json() or {}).get("calendars", {})
    entry = calendars.get(creds.calendar_id) or {}

    if entry.get("errors"):
        reason = (entry["errors"][0] or {}).get("reason", "unknown")
        raise GoogleCalendarError(
            f"calendar unreadable: {reason}",
            permanent=reason in ("notFound", "forbidden"),
        )

    out = []
    for span in entry.get("busy", []) or []:
        s, e = parse_iso_aware(span.get("start", "")), parse_iso_aware(span.get("end", ""))
        if s and e:
            out.append((s, e))
    return out


async def insert_event(
    creds: GoogleCreds,
    *,
    start,
    end,
    summary: str,
    description: str = "",
    timezone_name: str = "UTC",
    attendee_email: str = "",
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Create the event. Idempotent via a caller-stable request id."""
    token = await access_token(creds, client=client)
    event: dict = {
        "summary": summary[:400],
        "description": description[:4000],
        "start": {"dateTime": to_rfc3339(start), "timeZone": timezone_name},
        "end": {"dateTime": to_rfc3339(end), "timeZone": timezone_name},
    }
    if attendee_email:
        event["attendees"] = [{"email": attendee_email}]

    headers = {"Authorization": f"Bearer {token}"}
    path = f"{API_BASE}/calendars/{creds.calendar_id}/events"

    async def _post(c: httpx.AsyncClient):
        return await c.post(path, json=event, headers=headers)

    if client is not None:
        response = await _post(client)
    else:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            response = await _post(c)

    if response.status_code >= 400:
        raise GoogleCalendarError(
            f"event insert failed ({response.status_code})",
            status=response.status_code,
            permanent=response.status_code in (401, 403, 404),
        )

    body = response.json() or {}
    return {
        "id": body.get("id") or uuid.uuid4().hex[:12],
        "html_link": body.get("htmlLink", ""),
        "status": body.get("status", "confirmed"),
    }
