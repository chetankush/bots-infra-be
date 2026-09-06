"""Transcripts belong in the database, not in log aggregation."""

from app.logging import _redact


def _r(**kw):
    return _redact(None, None, dict(kw))


def test_pii_is_redacted_from_free_text():
    out = _r(msg="reach me on +91 79874 01227 or someone@example.com")
    assert "79874" not in out["msg"] and "@example.com" not in out["msg"]
    assert "[phone]" in out["msg"] and "[email]" in out["msg"]


def test_structural_fields_survive():
    out = _r(timestamp="2026-09-05T16:32:41.123456Z", url="https://x.com/a/b", latency_ms="1234")
    assert out["timestamp"] == "2026-09-05T16:32:41.123456Z"
    assert out["url"] == "https://x.com/a/b"
