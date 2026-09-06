"""Tools are config-gated, and every one has a mock so evals can't touch live systems."""

import pytest

from app.tools.registry import build_registry


@pytest.fixture
def reg():
    return build_registry()


def test_every_tool_has_a_mock(reg):
    for name in ["search_availability", "book_appointment", "capture_lead", "escalate_to_human"]:
        spec = reg.get(name)
        assert spec is not None, f"{name} not registered"
        assert spec.mock_executor is not None
        assert spec.mock_executor is not spec.executor


def test_schemas_only_include_enabled_tools(reg):
    schemas = reg.schemas_for(["capture_lead"])
    assert [s["function"]["name"] for s in schemas] == ["capture_lead"]
    assert reg.schemas_for([]) == []


async def test_unknown_tool_returns_error_not_exception(reg):
    out = await reg.execute("nope", {}, {}, use_mock=True)
    assert out["ok"] is False and "unknown tool" in out["error"]


async def test_tool_failure_is_data_not_a_crashed_turn(reg):
    # book_appointment with a junk datetime must not raise
    out = await reg.execute(
        "book_appointment",
        {"starts_at": "not-a-date", "name": "x", "phone": "1"},
        {"tenant_id": None, "conversation_id": None},
        use_mock=False,
    )
    assert out["ok"] is False


async def test_mocks_are_deterministic(reg):
    a = await reg.execute("search_availability", {}, {}, use_mock=True)
    b = await reg.execute("search_availability", {}, {}, use_mock=True)
    assert a["slots"] == b["slots"]
