"""Lead capture - the tool that makes the client money."""

from __future__ import annotations

from app.db.models import Lead
from app.db.session import session_scope
from app.services.email import enqueue_email, lead_body
from app.services.webhooks import enqueue
from app.tools.registry import ToolSpec, registry


async def _mock(*, args: dict, ctx: dict) -> dict:
    return {"ok": True, "lead_id": "MOCK-LEAD-001", "captured": args.get("fields", {})}


async def _real(*, args: dict, ctx: dict) -> dict:
    fields = args.get("fields") or {k: v for k, v in args.items() if k != "fields"}
    if not fields:
        return {"ok": False, "error": "no fields provided"}

    async with session_scope() as session:
        lead = Lead(
            tenant_id=ctx["tenant_id"],
            location_id=ctx.get("location_id"),
            conversation_id=ctx["conversation_id"],
            fields=fields,
        )
        session.add(lead)
        await session.flush()
        lead_id = str(lead.id)

    cfg = ctx.get("config") or {}
    await enqueue_email(
        tenant_id=ctx["tenant_id"],
        to=cfg.get("lead_email_to") or [],
        subject=f"New lead - {cfg.get('business_name') or 'website'}",
        text=lead_body(business=cfg.get("business_name") or "your", fields=fields),
        reply_to=(cfg.get("escalation") or {}).get("reply_to", ""),
        kind="lead",
    )
    await enqueue(
        tenant_id=ctx["tenant_id"],
        event="lead.captured",
        payload={
            "lead_id": lead_id,
            "conversation_id": str(ctx["conversation_id"]),
            "fields": fields,
        },
        url=cfg.get("lead_webhook_url", ""),
        secret=cfg.get("lead_webhook_secret", ""),
    )
    return {"ok": True, "lead_id": lead_id, "captured": fields}


registry.register(
    ToolSpec(
        name="capture_lead",
        description=(
            "Save the customer's details once you have collected the required fields. "
            "Call this as soon as you have enough - do not wait for the end of the chat."
        ),
        parameters={
            "type": "object",
            "properties": {
                "fields": {
                    "type": "object",
                    "description": "Collected values keyed by the configured lead field keys",
                    "additionalProperties": {"type": "string"},
                }
            },
            "required": ["fields"],
        },
        executor=_real,
        mock_executor=_mock,
    )
)
