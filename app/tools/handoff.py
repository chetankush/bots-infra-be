"""Human escalation, with full conversation context attached."""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import Conversation, Message
from app.db.session import session_scope
from app.logging import get_logger
from app.services.email import enqueue_email, escalation_body
from app.services.webhooks import enqueue
from app.tools.registry import ToolSpec, registry

log = get_logger("handoff")


async def _mock(*, args: dict, ctx: dict) -> dict:
    return {"ok": True, "escalated": True, "reason": args.get("reason", "")}


async def _real(*, args: dict, ctx: dict) -> dict:
    async with session_scope() as session:
        conversation = await session.get(Conversation, ctx["conversation_id"])
        if conversation and conversation.tenant_id == ctx["tenant_id"]:
            conversation.status = "escalated"

    cfg = ctx.get("config") or {}
    escalation = cfg.get("escalation") or {}

    # Transcript for the human picking this up. Bounded: an escalation email is a
    # handover note, not an archive.
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(Message)
                    .where(
                        Message.tenant_id == ctx["tenant_id"],
                        Message.conversation_id == ctx["conversation_id"],
                        Message.role.in_(("user", "assistant")),
                    )
                    .order_by(Message.created_at.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
    transcript = "\n".join(
        f"{'Visitor' if m.role == 'user' else 'Assistant'}: {m.content[:600]}"
        for m in reversed(rows)
    )

    await enqueue_email(
        tenant_id=ctx["tenant_id"],
        to=escalation.get("email_to") or [],
        subject=f"Assistant handover - {cfg.get('business_name') or 'website visitor'}",
        text=escalation_body(
            business=cfg.get("business_name") or "your",
            reason=str(args.get("reason", "")),
            summary=str(args.get("summary", "")),
            intake=ctx.get("intake") or {},
            transcript=transcript,
        ),
        reply_to=escalation.get("reply_to", ""),
        kind="escalation",
    )
    await enqueue(
        tenant_id=ctx["tenant_id"],
        event="conversation.escalated",
        payload={
            "conversation_id": str(ctx["conversation_id"]),
            "reason": args.get("reason", ""),
            "summary": args.get("summary", ""),
            "intake": ctx.get("intake", {}),
        },
        url=escalation.get("webhook_url") or "",
        secret=escalation.get("webhook_secret", ""),
    )
    log.info(
        "escalated",
        tenant_id=str(ctx["tenant_id"]),
        conversation_id=str(ctx["conversation_id"]),
        reason=args.get("reason", ""),
    )
    return {"ok": True, "escalated": True, "reason": args.get("reason", "")}


registry.register(
    ToolSpec(
        name="escalate_to_human",
        description=(
            "Hand the conversation to a human team member. Use when the customer asks for a "
            "person, is unhappy, or asks something you are not allowed to answer."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "summary": {
                    "type": "string",
                    "description": "Short summary for the human picking this up",
                },
            },
            "required": ["reason"],
        },
        executor=_real,
        mock_executor=_mock,
    )
)
