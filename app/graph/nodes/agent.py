"""The answering node, plus tool execution."""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.config.schema import AgentConfig
from app.graph.prompts import context_block, system_prefix
from app.graph.state import AgentState
from app.llm.factory import get_model
from app.logging import get_logger
from app.rag.retriever import format_context
from app.settings import get_settings
from app.tools.registry import build_registry

log = get_logger("agent")


def _build_messages(state: AgentState, cfg: AgentConfig) -> list:
    """Stable prefix first (cacheable), volatile content last."""
    # cache_control MUST sit inside a content block. Put it in additional_kwargs on
    # the message and langchain-openai drops it silently - the request still works, it
    # just never caches, and the only symptom is a bill that is ~4x higher than it
    # should be. Verified on the wire with _convert_message_to_dict.
    prefix = SystemMessage(
        content=[
            {
                "type": "text",
                "text": system_prefix(
                    cfg,
                    channel=state.get("channel", "web"),
                    locale=state.get("locale", "en"),
                ),
                "cache_control": {"type": "ephemeral", "ttl": cfg.models.cache_ttl},
            }
        ]
    )

    history = list(state["messages"])
    context = format_context(state.get("retrieved", []))

    # Fold retrieved context into the latest human turn only, so earlier turns stay
    # byte-stable and the cached prefix keeps extending.
    for i in range(len(history) - 1, -1, -1):
        if isinstance(history[i], HumanMessage):
            history[i] = HumanMessage(content=context_block(context, str(history[i].content)))
            break

    intake = state.get("intake") or {}
    if intake:
        history.insert(
            0,
            SystemMessage(
                content=f"Already collected: {json.dumps(intake)}. Do not ask for these again."
            ),
        )
    return [prefix, *history]


async def agent(state: AgentState) -> dict:
    cfg = AgentConfig.model_validate(state["config"])
    iterations = state.get("iterations", 0) + 1

    if state.get("blocked"):
        return {
            "messages": [AIMessage(content=cfg.refusal_message)],
            "trace": ["agent:refused"],
            "iterations": iterations,
        }

    reg = build_registry()
    schemas = reg.schemas_for(cfg.tools)
    role = "escalate" if state.get("escalate") else "answer"
    model = get_model(role, cfg.models)
    if schemas:
        model = model.bind_tools(schemas)

    response = await model.ainvoke(_build_messages(state, cfg))

    usage = dict(state.get("usage") or {"input": 0, "output": 0})
    meta = getattr(response, "usage_metadata", None) or {}
    usage["input"] += int(meta.get("input_tokens", 0) or 0)
    usage["output"] += int(meta.get("output_tokens", 0) or 0)

    return {
        "messages": [response],
        "usage": usage,
        "trace": ["agent"],
        "iterations": iterations,
    }


async def tools(state: AgentState) -> dict:
    cfg = AgentConfig.model_validate(state["config"])
    reg = build_registry()
    settings = get_settings()

    last = state["messages"][-1]
    calls = getattr(last, "tool_calls", None) or []
    if not calls:
        return {"trace": ["tools"]}

    ctx = {
        "tenant_id": state["tenant_id"],
        "location_id": state.get("location_id"),
        "conversation_id": state["conversation_id"],
        "config": state.get("config") or {},
        "intake": state.get("intake") or {},
    }

    out_messages, intake = [], dict(state.get("intake") or {})
    escalated = state.get("escalate", False)

    for call in calls:
        name = call.get("name", "")
        args = call.get("args", {}) or {}

        if name not in cfg.tools:
            result = {"ok": False, "error": f"tool '{name}' is not enabled for this tenant"}
        else:
            result = await reg.execute(name, args, ctx, use_mock=settings.use_mock_tools)

        if name == "capture_lead" and result.get("ok"):
            intake.update(result.get("captured") or {})
        if name == "escalate_to_human" and result.get("ok"):
            escalated = True

        log.info("tool_executed", tool=name, ok=result.get("ok"), mock=settings.use_mock_tools)
        out_messages.append(
            ToolMessage(
                content=json.dumps(result, default=str),
                tool_call_id=call.get("id", name),
                name=name,
            )
        )

    return {
        "messages": out_messages,
        "intake": intake,
        "escalate": escalated,
        "trace": ["tools"],
    }
