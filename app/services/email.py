"""Transactional email via Resend.

Queued through the existing Postgres job queue, never sent inline: a slow mail API must
not add latency to a visitor's turn, and a failed send must not fail the conversation.

Three deliberate choices:
- The API key is platform-level, not per tenant. The agency sends on behalf of many
  clients from its own verified domain; asking each client for a Resend account would
  block onboarding. A tenant may override the reply-to so replies reach them directly.
- The dev sink logs metadata only. Printing a rendered email would put a full chat
  transcript through the logger, which is exactly what the PII redaction exists to stop.
- Every recipient is validated. Addresses reach here from tenant config and, for
  customer mail, from LLM tool arguments.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import httpx

from app.db.models import JobQueue
from app.db.session import session_scope
from app.logging import get_logger
from app.settings import get_settings

log = get_logger("email")

RESEND_URL = "https://api.resend.com/emails"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)

_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,24}$")

# A single misconfigured tenant must not be able to spend the agency's whole quota.
MAX_RECIPIENTS = 10
MAX_BODY_CHARS = 20_000


def valid_email(raw: str) -> str:
    value = (raw or "").strip()
    return value if _EMAIL.match(value) else ""


def clean_recipients(raw: list[str] | None) -> list[str]:
    seen, out = set(), []
    for item in raw or []:
        address = valid_email(str(item))
        if address and address.lower() not in seen:
            seen.add(address.lower())
            out.append(address)
        if len(out) >= MAX_RECIPIENTS:
            break
    return out


async def enqueue_email(
    *,
    tenant_id: uuid.UUID,
    to: list[str],
    subject: str,
    text: str,
    reply_to: str = "",
    kind: str = "notification",
) -> bool:
    recipients = clean_recipients(to)
    if not recipients:
        log.info("email_skipped_no_recipients", kind=kind, tenant_id=str(tenant_id))
        return False

    async with session_scope() as session:
        session.add(
            JobQueue(
                tenant_id=tenant_id,
                kind="email",
                payload={
                    "id": uuid.uuid4().hex,
                    "to": recipients,
                    "subject": subject[:200],
                    "text": text[:MAX_BODY_CHARS],
                    "reply_to": valid_email(reply_to),
                    "kind": kind,
                },
            )
        )
    log.info("email_queued", kind=kind, recipients=len(recipients), tenant_id=str(tenant_id))
    return True


async def deliver_email(payload: dict[str, Any]) -> dict:
    """Send one queued email. Raises on a retryable failure so the worker backs off."""
    settings = get_settings()
    recipients = clean_recipients(payload.get("to"))
    if not recipients:
        return {"ok": False, "permanent": True, "error": "no valid recipients"}

    if not settings.resend_api_key:
        # Dev sink: metadata only. The body is a chat transcript - it does not go to logs.
        log.info(
            "email_sink",
            kind=payload.get("kind"),
            recipients=len(recipients),
            subject_len=len(payload.get("subject", "")),
            body_len=len(payload.get("text", "")),
            note="RESEND_API_KEY unset; not sent",
        )
        return {"ok": True, "sink": True}

    body = {
        "from": settings.email_from,
        "to": recipients,
        "subject": payload.get("subject", "")[:200],
        "text": payload.get("text", "")[:MAX_BODY_CHARS],
    }
    if payload.get("reply_to"):
        body["reply_to"] = payload["reply_to"]

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.post(
            RESEND_URL,
            json=body,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
        )

    if 400 <= response.status_code < 500 and response.status_code != 429:
        log.warning("email_rejected", status=response.status_code, kind=payload.get("kind"))
        return {"ok": False, "permanent": True, "status": response.status_code}

    response.raise_for_status()
    log.info("email_sent", status=response.status_code, kind=payload.get("kind"))
    return {"ok": True, "status": response.status_code}


# ------------------------------------------------------------------ message bodies


def escalation_body(
    *, business: str, reason: str, summary: str, intake: dict, transcript: str
) -> str:
    lines = [
        f"A visitor on the {business} assistant needs a person.",
        "",
        f"Reason: {reason or 'not given'}",
    ]
    if summary:
        lines += ["", f"Summary: {summary}"]
    if intake:
        lines += ["", "Details collected:"]
        lines += [f"  {k}: {v}" for k, v in intake.items()]
    if transcript:
        lines += ["", "Conversation:", transcript]
    return "\n".join(lines)


def lead_body(*, business: str, fields: dict) -> str:
    lines = [f"New lead from the {business} assistant.", ""]
    lines += [f"  {k}: {v}" for k, v in (fields or {}).items()]
    return "\n".join(lines)


def appointment_body(*, business: str, starts_at: str, service: str, confirmed: bool) -> str:
    """Wording depends on whether the booking is actually confirmed.

    A booking that only exists in our own database must not be described to a customer
    as confirmed - they would turn up to a slot the client never saw.
    """
    if confirmed:
        opening = f"Your appointment with {business} is confirmed."
        closing = "If you need to change it, just reply to this email."
    else:
        opening = f"We've received your request with {business}."
        closing = "The team will confirm this shortly."
    return "\n".join(
        [
            opening,
            "",
            f"  When: {starts_at}",
            f"  Service: {service or 'not specified'}",
            "",
            closing,
        ]
    )
