"""Webhooks are the integration surface for Zapier / n8n / Power Automate."""

import hashlib
import hmac
import json

from app.rag.worker import _backoff
from app.services.webhooks import sign


def test_signature_is_verifiable_by_the_receiver():
    secret, body, ts = "whsec_test", b'{"event":"lead.captured"}', "1757000000"
    header = sign(secret, body, ts)

    parts = dict(p.split("=", 1) for p in header.split(","))
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()

    assert parts["t"] == ts
    assert hmac.compare_digest(parts["v1"], expected)


def test_timestamp_is_inside_the_signed_payload():
    """Otherwise a captured delivery could be replayed later."""
    secret, body = "s", b"{}"
    assert sign(secret, body, "1") != sign(secret, body, "2")


def test_tampered_body_breaks_the_signature():
    secret, ts = "s", "1757000000"
    good = sign(secret, json.dumps({"amount": 1}).encode(), ts)
    bad = sign(secret, json.dumps({"amount": 999}).encode(), ts)
    assert good != bad


def test_backoff_grows_and_is_capped():
    mins = [_backoff(i).total_seconds() / 60 for i in range(1, 7)]
    assert mins == [1, 4, 9, 16, 16, 16]


def test_no_reserved_structlog_kwarg_in_log_calls():
    """structlog owns `event=` - passing it as a field raises at call time, and only
    when that branch actually runs. Cheap to assert statically."""
    import pathlib
    import re

    for path in pathlib.Path("app").rglob("*.py"):
        for line in path.read_text().splitlines():
            if re.search(r"\blog\.(debug|info|warning|error)\([^)]*\bevent=", line):
                raise AssertionError(f"{path}: reserved kwarg `event=` in {line.strip()}")


async def test_4xx_is_permanent_and_5xx_retries(monkeypatch):
    """A rejected delivery must stop; a server error must be retried."""
    import httpx

    from app.services import webhooks

    class FakeResponse:
        def __init__(self, code):
            self.status_code = code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("boom", request=None, response=None)

    class FakeClient:
        def __init__(self, code):
            self.code = code

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return FakeResponse(self.code)

    job = {"id": "e", "url": "http://x/y", "secret": "", "event": "t", "data": {}}

    monkeypatch.setattr(webhooks.httpx, "AsyncClient", lambda **kw: FakeClient(400))
    assert (await webhooks.deliver(job))["permanent"] is True

    monkeypatch.setattr(webhooks.httpx, "AsyncClient", lambda **kw: FakeClient(429))
    try:
        await webhooks.deliver(job)
        raise AssertionError("429 should raise so the worker backs off")
    except httpx.HTTPStatusError:
        pass

    monkeypatch.setattr(webhooks.httpx, "AsyncClient", lambda **kw: FakeClient(503))
    try:
        await webhooks.deliver(job)
        raise AssertionError("503 should raise so the worker backs off")
    except httpx.HTTPStatusError:
        pass
