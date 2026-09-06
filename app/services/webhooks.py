"""Outbound webhooks.

The integration surface for Zapier, n8n, Make, Power Automate and any CRM that
accepts an HTTP callback - one feature instead of one bespoke connector per tool.

Deliveries are signed and retried. They never run inline with the visitor's turn:
a slow or dead endpoint on the client's side must not add latency to the chat, so
events are queued in Postgres and drained by the worker.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import httpx

from app.db.models import JobQueue
from app.db.session import session_scope
from app.logging import get_logger

log = get_logger("webhooks")

TIMEOUT_SECONDS = 10
MAX_ATTEMPTS = 5


def sign(secret: str, body: bytes, timestamp: str) -> str:
    """HMAC-SHA256 over `timestamp.body`.

    The timestamp is inside the signed payload so a captured delivery cannot be
    replayed later against the receiver.
    """
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


async def enqueue(
    *,
    tenant_id: uuid.UUID,
    event: str,
    payload: dict[str, Any],
    url: str,
    secret: str = "",
) -> None:
    """Queue a delivery. Cheap, non-blocking, survives a restart."""
    if not url:
        return
    async with session_scope() as session:
        session.add(
            JobQueue(
                tenant_id=tenant_id,
                kind="webhook",
                payload={
                    "url": url,
                    "secret": secret,
                    "event": event,
                    "data": payload,
                    "id": uuid.uuid4().hex,
                },
            )
        )
    log.info("webhook_queued", hook=event, tenant_id=str(tenant_id))


async def deliver(job_payload: dict) -> dict:
    """Send one delivery. Raises on a retryable failure so the worker backs off."""
    url = job_payload["url"]
    body = json.dumps(
        {
            "id": job_payload.get("id"),
            "event": job_payload["event"],
            "created_at": int(time.time()),
            "data": job_payload.get("data", {}),
        },
        default=str,
    ).encode()

    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "FirstVoidEngine/1.0",
        "X-FirstVoid-Event": job_payload["event"],
    }
    secret = job_payload.get("secret") or ""
    if secret:
        headers["X-FirstVoid-Signature"] = sign(secret, body, timestamp)

    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        response = await client.post(url, content=body, headers=headers)

    # 4xx (except 429) means the receiver rejected it - retrying will not help.
    if 400 <= response.status_code < 500 and response.status_code != 429:
        log.warning("webhook_rejected", status=response.status_code, hook=job_payload["event"])
        return {"ok": False, "status": response.status_code, "permanent": True}

    response.raise_for_status()
    log.info("webhook_delivered", status=response.status_code, hook=job_payload["event"])
    return {"ok": True, "status": response.status_code}
