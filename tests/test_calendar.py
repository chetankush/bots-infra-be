"""Google Calendar: exercised end to end against a local fake API.

No Google account, no network. The fake speaks the real wire protocol, including the
failure Google actually returns on a bad calendar id: HTTP 200 with a per-calendar
`errors` array, which is the shape that silently double-books if you mishandle it.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.integrations import google_calendar as gcal
from app.integrations.google_calendar import GoogleCalendarError, GoogleCreds

CREDS = GoogleCreds(
    client_id="cid.apps.googleusercontent.com",
    client_secret="secret",
    refresh_token="1//refresh",
    calendar_id="primary",
)


def _fake_api(*, token_status=200, fb_status=200, fb_body=None, insert_status=200):
    """A transport that answers Google's endpoints locally."""
    calls = {"token": 0, "freebusy": 0, "insert": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "oauth2.googleapis.com/token" in url:
            calls["token"] += 1
            if token_status != 200:
                return httpx.Response(token_status, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "ya29.fake", "expires_in": 3600})
        if url.endswith("/freeBusy"):
            calls["freebusy"] += 1
            if fb_status != 200:
                return httpx.Response(fb_status, json={"error": {"message": "nope"}})
            return httpx.Response(200, json=fb_body or {"calendars": {"primary": {"busy": []}}})
        if "/events" in url:
            calls["insert"] += 1
            if insert_status != 200:
                return httpx.Response(insert_status, json={"error": {"message": "nope"}})
            return httpx.Response(
                200,
                json={
                    "id": "evt_local_1",
                    "htmlLink": "https://cal/evt_local_1",
                    "status": "confirmed",
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


@pytest.fixture(autouse=True)
def _clear_token_cache():
    gcal._TOKENS.clear()
    yield
    gcal._TOKENS.clear()


async def test_token_refresh_and_caching():
    transport, calls = _fake_api()
    async with httpx.AsyncClient(transport=transport) as client:
        a = await gcal.access_token(CREDS, client=client)
        b = await gcal.access_token(CREDS, client=client)
    assert a == b == "ya29.fake"
    assert calls["token"] == 1, "second call must be served from cache"


async def test_revoked_token_is_permanent_not_retried():
    transport, _ = _fake_api(token_status=400)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(GoogleCalendarError) as exc:
            await gcal.access_token(CREDS, client=client)
    assert exc.value.permanent is True


async def test_token_cache_is_keyed_on_the_refresh_token():
    """Rotating a credential must invalidate the cache, not serve the stale token."""
    rotated = GoogleCreds(CREDS.client_id, CREDS.client_secret, "1//DIFFERENT", "primary")
    assert CREDS.cache_key != rotated.cache_key


async def test_free_busy_parses_intervals():
    now = datetime.now(UTC)
    body = {
        "calendars": {
            "primary": {
                "busy": [
                    {
                        "start": now.isoformat().replace("+00:00", "Z"),
                        "end": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                    }
                ]
            }
        }
    }
    transport, _ = _fake_api(fb_body=body)
    async with httpx.AsyncClient(transport=transport) as client:
        busy = await gcal.free_busy(CREDS, start=now, end=now + timedelta(days=1), client=client)
    assert len(busy) == 1
    assert busy[0][1] - busy[0][0] == timedelta(hours=1)


async def test_per_calendar_error_raises_instead_of_reading_as_free():
    """Google answers 200 with an errors array. Treating that as 'no busy periods'
    would present every slot as open and double-book the whole day."""
    body = {"calendars": {"primary": {"errors": [{"reason": "notFound"}]}}}
    transport, _ = _fake_api(fb_body=body)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(GoogleCalendarError) as exc:
            await gcal.free_busy(
                CREDS,
                start=datetime.now(UTC),
                end=datetime.now(UTC) + timedelta(days=1),
                client=client,
            )
    assert exc.value.permanent is True


async def test_insert_event_returns_the_reference():
    now = datetime.now(UTC) + timedelta(days=1)
    transport, calls = _fake_api()
    async with httpx.AsyncClient(transport=transport) as client:
        created = await gcal.insert_event(
            CREDS,
            start=now,
            end=now + timedelta(hours=1),
            summary="Service - Arjun",
            description="d",
            timezone_name="Asia/Kolkata",
            attendee_email="a@b.co",
            client=client,
        )
    assert created["id"] == "evt_local_1"
    assert calls["insert"] == 1


async def test_creds_from_incomplete_payload_is_none():
    assert GoogleCreds.from_payload({"client_id": "x"}) is None
    assert GoogleCreds.from_payload({}) is None
    ok = GoogleCreds.from_payload({"client_id": "a", "client_secret": "b", "refresh_token": "c"})
    assert ok is not None and ok.calendar_id == "primary"


def test_attendee_email_is_validated_not_trusted():
    """The address originates in LLM tool arguments, i.e. in a stranger's message."""
    from app.tools.booking import _safe_email

    assert _safe_email("real@example.com") == "real@example.com"
    for bad in ("not-an-email", "<script>@x.com", "a@b", "", "  "):
        assert _safe_email(bad) == ""


def test_evals_force_mocks_regardless_of_environment():
    """A prod-env eval replay must never write to a client's real calendar."""
    import pathlib

    src = pathlib.Path("app/evals/runner.py").read_text()
    assert "force_mock_tools = True" in src

    settings = pathlib.Path("app/settings.py").read_text()
    assert "force_mock_tools" in settings
    assert "self.force_mock_tools or self.env" in settings
