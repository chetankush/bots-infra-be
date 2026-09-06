"""Twilio request signing and outbound messaging.

Signature validation is pure and independently testable - no account required.

Two properties of Twilio's scheme drive the design here:
- The signature covers the URL plus the POST parameters, so the URL the app *believes*
  it is serving must match what Twilio signed. Behind a proxy that means trusting
  X-Forwarded-Proto/Host, which is why the public base URL is configuration.
- The signature contains no timestamp, so a captured request stays replayable forever.
  Signature validity alone is therefore not sufficient - inbound message ids are
  recorded and deduped (see InboundMessage).
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import httpx

from app.logging import get_logger

log = get_logger("twilio")

API_BASE = "https://api.twilio.com/2010-04-01"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def expected_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """HMAC-SHA1 over url + each key/value concatenated in sorted key order."""
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def valid_signature(auth_token: str, url: str, params: dict[str, str], header: str) -> bool:
    if not auth_token or not header:
        return False
    return hmac.compare_digest(expected_signature(auth_token, url, params), header)


async def send_message(
    *,
    account_sid: str,
    auth_token: str,
    from_address: str,
    to_address: str,
    body: str,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Send one WhatsApp/SMS message. Raises httpx errors so the worker can retry."""
    data = {"From": from_address, "To": to_address, "Body": body[:1600]}
    path = f"{API_BASE}/Accounts/{account_sid}/Messages.json"

    async def _post(c: httpx.AsyncClient):
        return await c.post(path, data=data, auth=(account_sid, auth_token))

    if client is not None:
        response = await _post(client)
    else:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            response = await _post(c)

    if 400 <= response.status_code < 500 and response.status_code != 429:
        log.warning("twilio_rejected", status=response.status_code)
        return {"ok": False, "permanent": True, "status": response.status_code}

    response.raise_for_status()
    body_json = response.json() if response.content else {}
    return {"ok": True, "sid": body_json.get("sid", ""), "status": response.status_code}
