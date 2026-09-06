"""Visitor-facing chat.

Two transports, one behaviour:

  POST /v1/chat         JSON. The default and the simpler path.
  POST /v1/chat/stream  SSE. Only worth it for a tenant that has opted into
                        streaming tokens before guard_out has cleared them.

Because replies are held until guard_out passes for any tenant with prohibitions,
streaming buys nothing for most clients - so the plain endpoint is the primary one.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from app.channels.envelope import Envelope
from app.db.session import session_scope
from app.graph.build import build_graph
from app.logging import get_logger
from app.services import ratelimit
from app.services.tenants import TenantError, check_budget, resolve_tenant
from app.services.turn import close_turn, extract_reply, initial_state, open_turn

router = APIRouter(tags=["chat"])
log = get_logger("chat")

_graph = build_graph()

FRIENDLY_ERROR = "Sorry, I had trouble with that. Could you try again?"


def _client_key(request: Request) -> str:
    """Hash the IP - it is a network identifier we never need in plaintext."""
    host = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        host = forwarded.split(",")[0].strip()
    return hashlib.sha256(host.encode()).hexdigest()[:24]


async def _prepare(envelope: Envelope, request: Request, origin: str | None):
    """Resolve tenant, enforce limits, persist the visitor turn.

    Raises TenantError, which both endpoints render in their own transport.
    """
    ip_key = _client_key(request)

    if not ratelimit.per_ip.allow(ip_key):
        raise TenantError("too many requests", 429)
    if not ratelimit.per_session.allow(f"{envelope.public_key}:{envelope.session_id}"):
        raise TenantError("too many requests", 429)

    async with session_scope() as session:
        resolved = await resolve_tenant(
            session,
            public_key=envelope.public_key,
            origin=origin,
            channel=envelope.channel,
            location_key=envelope.location_key,
        )
        if not ratelimit.per_tenant.allow(str(resolved.tenant_id)):
            raise TenantError("too many requests", 429)
        check_budget(resolved.tenant)

    ctx = await open_turn(
        resolved=resolved,
        session_id=envelope.session_id,
        channel=envelope.channel,
        locale=envelope.locale,
        consent=envelope.consent,
        text=envelope.text,
        meta={"ip": ip_key},
    )
    return ctx


@router.post("/v1/chat", response_model=None)
async def chat(
    envelope: Envelope,
    request: Request,
    origin: str | None = Header(default=None, alias="Origin"),
) -> JSONResponse:
    started = time.perf_counter()
    try:
        ctx = await _prepare(envelope, request, origin)
    except TenantError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)

    try:
        final = await _graph.ainvoke(
            initial_state(ctx, text=envelope.text, channel=envelope.channel, locale=envelope.locale)
        )
        reply = extract_reply(final)
        payload = await close_turn(ctx, final, reply=reply, started=started)
        return JSONResponse(payload)
    except Exception as exc:
        log.error("chat_failed", error=str(exc), conversation_id=str(ctx.conversation_id))
        # Fail quietly - a broken bot must never render an error on a client's homepage.
        return JSONResponse({"reply": FRIENDLY_ERROR, "error": True, "trace": []})


def _sse(event: str, data: dict) -> dict:
    return {"event": event, "data": json.dumps(data, default=str)}


@router.post("/v1/chat/stream", response_model=None)
async def chat_stream(
    envelope: Envelope,
    request: Request,
    origin: str | None = Header(default=None, alias="Origin"),
) -> EventSourceResponse | JSONResponse:
    started = time.perf_counter()
    try:
        ctx = await _prepare(envelope, request, origin)
    except TenantError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)

    async def events() -> AsyncIterator[dict]:
        yield _sse("start", {"conversation_id": str(ctx.conversation_id)})
        final: dict = {}
        emitted = ""

        try:
            async for kind, payload in _graph.astream(
                initial_state(
                    ctx, text=envelope.text, channel=envelope.channel, locale=envelope.locale
                ),
                stream_mode=["updates", "messages"],
            ):
                if kind == "updates":
                    for node, update in (payload or {}).items():
                        yield _sse("node", {"node": node})
                        update = update or {}
                        # `trace` has an additive reducer; a plain dict merge would
                        # keep only the last node's delta.
                        merged = [*final.get("trace", []), *(update.get("trace") or [])]
                        final = {**final, **update, "trace": merged}

                elif kind == "messages":
                    chunk, meta = payload
                    if meta.get("langgraph_node") != "agent":
                        continue
                    text = getattr(chunk, "content", "")
                    if isinstance(text, str) and text:
                        emitted += text
                        # Only surface tokens live when the tenant accepted that they
                        # bypass guard_out.
                        if ctx.stream_tokens:
                            yield _sse("token", {"text": text})

            reply = extract_reply(final) or emitted
            yield _sse("done", await close_turn(ctx, final, reply=reply, started=started))

        except Exception as exc:
            log.error("chat_failed", error=str(exc), conversation_id=str(ctx.conversation_id))
            yield _sse("done", {"reply": FRIENDLY_ERROR, "error": True, "trace": []})

    return EventSourceResponse(
        events(),
        ping=15,
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # never let a proxy buffer an event stream
        },
    )


@router.get("/v1/chat/bootstrap", response_model=None)
async def bootstrap(
    public_key: str,
    request: Request,
    location_key: str | None = None,
    origin: str | None = Header(default=None, alias="Origin"),
) -> JSONResponse:
    """What the widget needs before the first message.

    Whether consent is required is decided HERE, from tenant region and config. The
    client is told what to show; it never gets to decide whether the gate applies.
    """
    if not ratelimit.per_ip.allow(_client_key(request)):
        return JSONResponse({"error": "too many requests"}, status_code=429)
    try:
        async with session_scope() as session:
            # location_key matters: a location can override the greeting and the
            # consent notice, and bootstrap is exactly what the visitor sees.
            resolved = await resolve_tenant(
                session,
                public_key=public_key,
                origin=origin,
                location_key=location_key,
            )
            cfg = resolved.config
            needs_consent = resolved.consent_required()
    except TenantError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)

    payload = {
        "agent_name": cfg.agent_name,
        "business_name": cfg.business_name,
        "greeting": cfg.greeting,
        "stream": cfg.channel.stream_before_guard,
        "session_id": uuid.uuid4().hex,
        "consent_required": needs_consent,
    }
    if needs_consent:
        payload["consent"] = {
            "notice": cfg.consent.notice,
            "accept_label": cfg.consent.accept_label,
            "policy_url": cfg.consent.policy_url,
            "policy_label": cfg.consent.policy_label,
            "version": cfg.consent.version,
        }
    return JSONResponse(payload)
