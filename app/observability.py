"""Langfuse tracing, gated on configuration. With no keys set it is a no-op.

One observation per turn carrying what the operational questions actually need:
tenant, channel, conversation, answer model, the graph's own node trace, token usage,
latency, and whether the guard blocked it. That answers "why was this tenant slow
yesterday" and "which model is burning the budget" from a dashboard instead of logs.

Deliberately NOT the LangChain callback integration. Langfuse's handler imports the
full `langchain` package, which moves langchain-core across a major version and
drags langgraph with it. A turn-level observation costs two function calls and keeps
the graph free of any tracing framework. Per-LLM-call spans are a follow-up if a
tenant ever needs them.
"""

from __future__ import annotations

import uuid
from functools import lru_cache
from typing import Any

from app.logging import get_logger
from app.settings import get_settings

log = get_logger("observability")


@lru_cache(maxsize=1)
def _client():
    s = get_settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        return None
    from langfuse import Langfuse

    return Langfuse(
        public_key=s.langfuse_public_key,
        secret_key=s.langfuse_secret_key,
        host=s.langfuse_host,
        environment=s.env,
    )


def enabled() -> bool:
    return _client() is not None


def start_turn(
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    channel: str,
    model: str,
    text: str,
    tags: list[str] | None = None,
) -> Any | None:
    """Open the turn's observation. Returns None when tracing is off."""
    client = _client()
    if client is None:
        return None
    try:
        return client.start_observation(
            name="turn",
            as_type="agent",
            input=text,
            model=model,
            metadata={
                "tenant_id": str(tenant_id),
                "conversation_id": str(conversation_id),
                "channel": channel,
                "tags": tags or [],
            },
        )
    except Exception as exc:  # tracing must never take a turn down with it
        log.warning("trace_start_failed", error=str(exc))
        return None


def end_turn(obs: Any | None, *, reply: str, final: dict, latency_ms: int) -> None:
    """Close the observation with the outcome. Safe to call with None."""
    if obs is None:
        return
    usage = final.get("usage") or {}
    try:
        obs.update(
            output=reply,
            usage_details={
                "input": int(usage.get("input", 0)),
                "output": int(usage.get("output", 0)),
            },
            metadata={
                "trace": final.get("trace", []),
                "latency_ms": latency_ms,
                "iterations": final.get("iterations", 0),
                "blocked": bool(final.get("blocked", False)),
                "block_reason": final.get("block_reason", ""),
                "escalated": bool(final.get("escalate", False)),
            },
            level="WARNING" if final.get("blocked") else "DEFAULT",
        )
        obs.end()
    except Exception as exc:
        log.warning("trace_end_failed", error=str(exc))


def flush() -> None:
    client = _client()
    if client is not None:
        client.flush()
