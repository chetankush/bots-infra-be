"""Twilio WhatsApp/SMS inbound webhook.

The graph is channel-agnostic, so this file only translates: a signed Twilio POST
becomes an Envelope, runs the SAME turn pipeline as the web widget, and answers with
TwiML. There is no second graph and no duplicated business logic.

Security ordering matters and is deliberate:
 1. Route by the inbound `To` address. This is unvalidated data used only to pick a
    key - it authorises nothing on its own.
 2. Validate the Twilio signature with THAT tenant's auth token. A forged `To` reaches
    step 2 and fails it.
 3. Dedupe on MessageSid. Twilio retries on non-2xx and on timeouts, and its signature
    carries no timestamp, so signature validity alone cannot stop a replay.
 4. Rate limit per sender, after identification but before any model call.
"""

from __future__ import annotations

import uuid
from xml.sax.saxutils import escape

from fastapi import APIRouter, Header, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.channels.envelope import Envelope
from app.db.models import ChannelRoute, InboundMessage
from app.db.session import session_scope
from app.graph.build import build_graph
from app.integrations.twilio import valid_signature
from app.logging import get_logger
from app.services import ratelimit
from app.services.credentials import load_credential
from app.services.tenants import TenantError, check_budget, resolve_tenant
from app.services.turn import close_turn, extract_reply, initial_state, open_turn
from app.settings import get_settings

router = APIRouter(tags=["whatsapp"])
log = get_logger("whatsapp")

_graph = build_graph()

# Twilio expects TwiML. An empty <Response/> means "accepted, say nothing" - the right
# answer for a duplicate, a rejected sender, or an error we do not want to surface.
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response/>'


def _twiml(text: str) -> Response:
    if not text:
        return Response(EMPTY_TWIML, media_type="application/xml")
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Message>{escape(text)}</Message></Response>"
    )
    return Response(body, media_type="application/xml")


def _public_url(request: Request) -> str:
    """The URL Twilio signed.

    Behind Cloudflare and Caddy the app sees http://127.0.0.1:8000; Twilio signed the
    public https URL. Configuration wins over the request when it is set, because a
    forwarded header is attacker-influenced.
    """
    base = (get_settings().public_base_url or "").rstrip("/")
    if base:
        return f"{base}{request.url.path}"
    return str(request.url).split("?")[0]


@router.post("/v1/channels/twilio/inbound", response_model=None)
async def twilio_inbound(
    request: Request,
    x_twilio_signature: str | None = Header(default=None, alias="X-Twilio-Signature"),
) -> Response:
    form = dict((await request.form()).items())
    params = {k: str(v) for k, v in form.items()}

    to_address = params.get("To", "")
    from_address = params.get("From", "")
    sid = params.get("MessageSid", "") or params.get("SmsMessageSid", "")
    body = (params.get("Body", "") or "").strip()

    if not to_address or not from_address or not sid:
        return _twiml("")

    channel = "whatsapp" if to_address.startswith("whatsapp:") else "sms"

    # 1. route (unvalidated input, used only to select a key)
    async with session_scope() as session:
        route = (
            await session.execute(
                select(ChannelRoute).where(
                    ChannelRoute.channel == channel,
                    ChannelRoute.address == to_address,
                    ChannelRoute.active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if route is None:
            log.warning("twilio_unrouted", channel=channel)
            return _twiml("")

        tenant_id = route.tenant_id
        creds = await load_credential(session, tenant_id=tenant_id, provider="twilio")

    auth_token = (creds or {}).get("auth_token", "")

    # 2. authorise
    if not valid_signature(auth_token, _public_url(request), params, x_twilio_signature or ""):
        log.warning("twilio_bad_signature", channel=channel, tenant_id=str(tenant_id))
        return Response(EMPTY_TWIML, media_type="application/xml", status_code=403)

    # 3. dedupe - a replay or a Twilio retry must not produce a second reply
    try:
        async with session_scope() as session:
            session.add(InboundMessage(tenant_id=tenant_id, provider="twilio", provider_sid=sid))
    except IntegrityError:
        log.info("twilio_duplicate", tenant_id=str(tenant_id))
        return _twiml("")

    # 4. rate limit, keyed on the sender rather than an IP (Twilio's IP, not theirs)
    sender_key = f"wa:{tenant_id}:{from_address}"
    if not ratelimit.per_session.allow(sender_key) or not ratelimit.per_tenant.allow(
        str(tenant_id)
    ):
        log.warning("twilio_rate_limited", tenant_id=str(tenant_id))
        return _twiml("")

    if not body:
        return _twiml("")

    try:
        async with session_scope() as session:
            resolved = await resolve_tenant(
                session,
                public_key=await _public_key_for(session, tenant_id),
                origin=None,
                channel=channel,
            )
            check_budget(resolved.tenant)

        envelope = Envelope(
            public_key=resolved.tenant.public_key,
            # The sender's address IS the session. WhatsApp has no browser session,
            # and history should persist across days for the same person.
            session_id=f"tw{uuid.uuid5(uuid.NAMESPACE_URL, from_address).hex[:20]}",
            text=body[:4000],
            channel=channel,
            consent=True,  # messaging the business is the opt-in
        )

        ctx = await open_turn(
            resolved=resolved,
            session_id=envelope.session_id,
            channel=channel,
            locale=envelope.locale,
            consent=True,
            text=envelope.text,
            meta={"from": "whatsapp"},
        )
        import time

        started = time.perf_counter()
        final = await _graph.ainvoke(
            initial_state(ctx, text=envelope.text, channel=channel, locale=envelope.locale)
        )
        reply = extract_reply(final)
        await close_turn(ctx, final, reply=reply, started=started)
        return _twiml(reply)

    except TenantError as exc:
        log.info("twilio_refused", reason=str(exc), tenant_id=str(tenant_id))
        return _twiml("")
    except Exception as exc:
        log.error("twilio_failed", error=f"{type(exc).__name__}: {exc}"[:200])
        # Silence beats an error message on someone's phone.
        return _twiml("")


async def _public_key_for(session, tenant_id: uuid.UUID) -> str:
    from app.db.models import Tenant

    tenant = await session.get(Tenant, tenant_id)
    return tenant.public_key if tenant else ""
