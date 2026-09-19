"""Provider selection is a setting. The graph must not be able to tell the difference."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config.schema import ModelPolicy
from app.llm import factory


@pytest.fixture(autouse=True)
def reset_cache():
    factory._client.cache_clear()
    yield
    factory._client.cache_clear()


def _settings(**over):
    base = {
        "llm_provider": "openrouter",
        "openrouter_api_key": "",
        "openrouter_base_url": "https://openrouter.ai/api/v1",
        "openrouter_app_url": "https://firstvoid.com",
        "openrouter_app_title": "FirstVoid Engine",
        "ollama_base_url": "http://localhost:11434/v1",
    }
    return SimpleNamespace(**{**base, **over})


def test_openrouter_without_a_key_refuses_to_build(monkeypatch):
    monkeypatch.setattr(factory, "get_settings", lambda: _settings())
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        factory.get_model("answer", ModelPolicy())


def test_ollama_needs_no_openrouter_key_and_points_locally(monkeypatch):
    monkeypatch.setattr(factory, "get_settings", lambda: _settings(llm_provider="ollama"))
    policy = ModelPolicy(answer="llama3.2", fallbacks=["should-not-be-sent"])
    model = factory.get_model("answer", policy)

    assert model.model_name == "llama3.2"
    assert str(model.openai_api_base).startswith("http://localhost:11434")
    # OpenRouter-only body fields must not leak to a server that would reject them.
    assert not (model.extra_body or {}).get("models")


def test_openrouter_sends_fallbacks_and_attribution(monkeypatch):
    monkeypatch.setattr(factory, "get_settings", lambda: _settings(openrouter_api_key="k"))
    policy = ModelPolicy(
        answer="anthropic/claude-haiku-4.5", fallbacks=["google/gemini-2.5-flash-lite"]
    )
    model = factory.get_model("answer", policy)

    assert model.extra_body["models"] == [
        "anthropic/claude-haiku-4.5",
        "google/gemini-2.5-flash-lite",
    ]
    assert model.default_headers["X-Title"] == "FirstVoid Engine"


def test_guard_and_judge_run_cold(monkeypatch):
    monkeypatch.setattr(factory, "get_settings", lambda: _settings(llm_provider="ollama"))
    assert factory.get_model("guard", ModelPolicy(temperature=0.7)).temperature == 0.0
    assert factory.get_model("judge", ModelPolicy(temperature=0.7)).temperature == 0.0
