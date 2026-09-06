"""Email: queued not inline, recipients validated, bodies honest, PII kept out of logs."""

import pathlib

import httpx
import pytest

from app.services import email as mail
from app.services.email import (
    MAX_RECIPIENTS,
    appointment_body,
    clean_recipients,
    escalation_body,
    lead_body,
    valid_email,
)


def test_recipients_are_validated_deduped_and_capped():
    raw = ["a@b.co", "A@B.CO", "junk", "", None, "c@d.io"] + [f"x{i}@y.io" for i in range(30)]
    out = clean_recipients(raw)
    assert out[0] == "a@b.co"
    assert "junk" not in out
    assert len(out) <= MAX_RECIPIENTS
    assert len({o.lower() for o in out}) == len(out), "duplicates survived"


def test_invalid_addresses_are_rejected():
    for bad in ("plain", "a@b", "<script>@x.com", "", "   "):
        assert valid_email(bad) == ""


def test_appointment_wording_tracks_whether_it_is_actually_confirmed():
    """A booking that exists only in our own DB must not be called confirmed."""
    real = appointment_body(business="X", starts_at="Mon", service="s", confirmed=True)
    provisional = appointment_body(business="X", starts_at="Mon", service="s", confirmed=False)
    assert "confirmed" in real.lower()
    assert "confirmed" not in provisional.split("\n")[0].lower()
    assert "received your request" in provisional.lower()


def test_customer_email_is_off_by_default():
    """Shipping it on would email real customers without anyone deciding to."""
    from app.config.schema import AgentConfig

    assert AgentConfig().appointment_email_enabled is False


def test_dev_sink_logs_metadata_not_the_body():
    """The body is a chat transcript; logging it defeats the PII redaction."""
    src = pathlib.Path("app/services/email.py").read_text()
    sink = src.split("if not settings.resend_api_key:")[1].split("return {")[0]
    assert "body_len" in sink and "subject_len" in sink
    for leak in ("text=payload", "body=body", "print("):
        assert leak not in sink, f"dev sink leaks the message body via {leak}"


async def test_4xx_permanent_5xx_retries(monkeypatch):
    class R:
        def __init__(self, code):
            self.status_code = code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("boom", request=None, response=None)

    class C:
        def __init__(self, code):
            self.code = code

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return R(self.code)

    monkeypatch.setattr(mail.get_settings(), "resend_api_key", "re_test", raising=False)
    payload = {"to": ["a@b.co"], "subject": "s", "text": "t", "kind": "test"}

    monkeypatch.setattr(mail.httpx, "AsyncClient", lambda **kw: C(422))
    assert (await mail.deliver_email(payload))["permanent"] is True

    for retryable in (429, 500, 503):
        code = retryable
        monkeypatch.setattr(mail.httpx, "AsyncClient", (lambda c: lambda **kw: C(c))(code))
        with pytest.raises(httpx.HTTPStatusError):
            await mail.deliver_email(payload)


async def test_no_valid_recipients_is_permanent_not_retried():
    assert (await mail.deliver_email({"to": ["junk"], "kind": "t"}))["permanent"] is True


def test_bodies_include_what_a_human_needs():
    esc = escalation_body(
        business="Northside",
        reason="pricing dispute",
        summary="angry",
        intake={"name": "Arjun", "phone": "999"},
        transcript="Visitor: hi",
    )
    assert "pricing dispute" in esc and "Arjun" in esc and "Visitor: hi" in esc
    assert "Priya" in lead_body(business="X", fields={"name": "Priya"})


def test_email_is_queued_never_sent_inline():
    """A slow mail API must not add latency to a visitor's turn."""
    for tool in ("app/tools/handoff.py", "app/tools/lead.py"):
        src = pathlib.Path(tool).read_text()
        assert "enqueue_email" in src
        assert "deliver_email" not in src, f"{tool} sends inline"
