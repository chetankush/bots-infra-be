"""One conversational turn.

Both the JSON endpoint and the SSE endpoint run this - the transport differs, the
behaviour does not.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from app.db.models import Conversation, Message, Tenant
from app.db.session import session_scope
from app.logging import get_logger
from app.services.history import load_history
from app.services.language import resolve as resolve_locale
from app.services.tenants import ResolvedTenant, TenantError, get_or_create_conversation

log = get_logger("turn")


@dataclass
class TurnContext:
    tenant_id: uuid.UUID
    location_id: uuid.UUID | None
    conversation_id: uuid.UUID
    config: dict[str, Any]
    history: list = field(default_factory=list)
    intake: dict = field(default_factory=dict)
    stream_tokens: bool = False
    locale: str = "en"


async def open_turn(
    *,
    resolved: ResolvedTenant,
    session_id: str,
    channel: str,
    locale: str,
    consent: bool,
    text: str,
    meta: dict,
) -> TurnContext:
    """Persist the visitor message and gather everything the graph needs."""
    if resolved.consent_required() and not consent:
        # Refused BEFORE any row is written. Storing the message and then asking for
        # permission to store it defeats the point of asking.
        raise TenantError("consent required", 451)

    async with session_scope() as session:
        conversation = await get_or_create_conversation(
            session,
            resolved=resolved,
            session_id=session_id,
            channel=channel,
            locale=locale,
            consent=consent,
            meta=meta,
        )
        history = await load_history(
            session,
            tenant_id=resolved.tenant_id,
            conversation_id=conversation.id,
            max_turns=resolved.config.channel.max_turns,
        )
        session.add(
            Message(
                tenant_id=resolved.tenant_id,
                conversation_id=conversation.id,
                role="user",
                content=text,
            )
        )
        # Stamp on first acceptance. get_or_create_conversation already sets
        # `consent` at creation, so keying off `not conversation.consent` would skip
        # a brand-new consented conversation - the timestamp is the reliable sentinel.
        if consent and conversation.consent_at is None:
            conversation.consent = True
            conversation.consent_at = datetime.now(UTC)
            conversation.consent_version = resolved.config.consent.version

        # Detected once per turn from the visitor's own words. The client-declared
        # locale is a hint - a widget on a Spanish page still gets English visitors.
        locale_resolved = resolve_locale(
            declared=locale,
            text=text,
            supported=resolved.config.locales,
            default=(resolved.config.locales or ["en"])[0],
        )

        return TurnContext(
            tenant_id=resolved.tenant_id,
            location_id=resolved.location_id,
            conversation_id=conversation.id,
            config=resolved.config.model_dump(),
            history=history,
            intake=dict(conversation.intake or {}),
            stream_tokens=resolved.config.channel.stream_before_guard,
            locale=locale_resolved,
        )


def initial_state(ctx: TurnContext, *, text: str, channel: str, locale: str) -> dict:
    # ctx.locale is the resolved one; the argument is what the client claimed.
    return {
        "tenant_id": ctx.tenant_id,
        "location_id": ctx.location_id,
        "conversation_id": ctx.conversation_id,
        "channel": channel,
        "locale": ctx.locale or locale,
        "config": ctx.config,
        "messages": [*ctx.history, HumanMessage(content=text)],
        "intake": ctx.intake,
        "iterations": 0,
        "trace": [],
        "usage": {"input": 0, "output": 0},
    }


def extract_reply(final: dict) -> str:
    for message in reversed(final.get("messages", []) or []):
        if isinstance(message, AIMessage) and str(message.content).strip():
            return str(message.content)
    return ""


async def close_turn(ctx: TurnContext, final: dict, *, reply: str, started: float) -> dict:
    """Persist the reply, roll up usage, and return the client payload."""
    usage = final.get("usage") or {"input": 0, "output": 0}
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    async with session_scope() as session:
        session.add(
            Message(
                tenant_id=ctx.tenant_id,
                conversation_id=ctx.conversation_id,
                role="assistant",
                content=reply,
                meta={
                    "trace": final.get("trace", []),
                    "blocked": final.get("blocked", False),
                    "block_reason": final.get("block_reason", ""),
                    "latency_ms": elapsed_ms,
                },
                tokens_in=int(usage.get("input", 0)),
                tokens_out=int(usage.get("output", 0)),
            )
        )
        conversation = await session.get(Conversation, ctx.conversation_id)
        if conversation is not None:
            conversation.intake = final.get("intake") or ctx.intake
            if final.get("escalate"):
                conversation.status = "escalated"

        tenant = await session.get(Tenant, ctx.tenant_id)
        if tenant is not None:
            tenant.tokens_used_period += int(usage.get("input", 0)) + int(usage.get("output", 0))

    return {
        "conversation_id": str(ctx.conversation_id),
        "reply": reply,
        "trace": final.get("trace", []),
        "blocked": bool(final.get("blocked", False)),
        "escalated": bool(final.get("escalate", False)),
        "intake": final.get("intake", {}),
        "usage": usage,
        "latency_ms": elapsed_ms,
        "streamed": ctx.stream_tokens,
    }
