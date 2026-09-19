"""Tracing is optional and must never take a turn down with it."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app import observability


@pytest.fixture(autouse=True)
def reset_client_cache():
    observability._client.cache_clear()
    yield
    observability._client.cache_clear()


def _settings(**over):
    base = {
        "langfuse_public_key": "",
        "langfuse_secret_key": "",
        "langfuse_host": "https://cloud.langfuse.com",
        "env": "test",
    }
    return SimpleNamespace(**{**base, **over})


def test_no_keys_means_no_client_and_no_ops(monkeypatch):
    monkeypatch.setattr(observability, "get_settings", lambda: _settings())
    assert observability.enabled() is False
    obs = observability.start_turn(
        tenant_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        channel="web",
        model="m",
        text="hi",
    )
    assert obs is None
    observability.end_turn(None, reply="r", final={}, latency_ms=1)  # must not raise
    observability.flush()  # must not raise


def test_keys_build_a_client_and_open_an_observation(monkeypatch):
    monkeypatch.setattr(
        observability,
        "get_settings",
        lambda: _settings(langfuse_public_key="pk", langfuse_secret_key="sk"),
    )
    fake_obs = MagicMock()
    fake_client = MagicMock()
    fake_client.start_observation.return_value = fake_obs
    monkeypatch.setattr("langfuse.Langfuse", lambda **kw: fake_client)

    tid, cid = uuid.uuid4(), uuid.uuid4()
    obs = observability.start_turn(
        tenant_id=tid, conversation_id=cid, channel="whatsapp", model="claude", text="hello"
    )
    assert obs is fake_obs
    kwargs = fake_client.start_observation.call_args.kwargs
    assert kwargs["name"] == "turn" and kwargs["as_type"] == "agent"
    assert kwargs["metadata"]["tenant_id"] == str(tid)
    assert kwargs["metadata"]["channel"] == "whatsapp"


def test_end_turn_records_usage_trace_and_block_state(monkeypatch):
    monkeypatch.setattr(
        observability,
        "get_settings",
        lambda: _settings(langfuse_public_key="pk", langfuse_secret_key="sk"),
    )
    fake_obs = MagicMock()
    final = {
        "usage": {"input": 120, "output": 40},
        "trace": ["guard_in", "retrieve", "agent", "guard_out"],
        "blocked": True,
        "block_reason": "prohibited_topic",
        "iterations": 2,
    }
    observability.end_turn(fake_obs, reply="I can't help with that.", final=final, latency_ms=1100)

    kwargs = fake_obs.update.call_args.kwargs
    assert kwargs["usage_details"] == {"input": 120, "output": 40}
    assert kwargs["metadata"]["trace"] == final["trace"]
    assert kwargs["metadata"]["blocked"] is True
    assert kwargs["level"] == "WARNING"  # blocked turns are visible at a glance
    fake_obs.end.assert_called_once()


def test_a_broken_tracer_does_not_break_the_turn(monkeypatch):
    monkeypatch.setattr(
        observability,
        "get_settings",
        lambda: _settings(langfuse_public_key="pk", langfuse_secret_key="sk"),
    )
    exploding = MagicMock()
    exploding.start_observation.side_effect = RuntimeError("langfuse down")
    monkeypatch.setattr("langfuse.Langfuse", lambda **kw: exploding)

    obs = observability.start_turn(
        tenant_id=uuid.uuid4(), conversation_id=uuid.uuid4(), channel="web", model="m", text="x"
    )
    assert obs is None  # degraded to no-op, no exception

    bad_obs = MagicMock()
    bad_obs.update.side_effect = RuntimeError("boom")
    observability.end_turn(bad_obs, reply="r", final={}, latency_ms=1)  # swallowed, logged
