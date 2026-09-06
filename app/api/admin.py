"""Agency-side admin API.

Onboarding a client is: create tenant -> crawl their site -> hand over the snippet.
No fork, no deploy.
"""

from __future__ import annotations

import secrets
import uuid

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text

from app.config.packs import PACKS
from app.config.resolver import invalidate
from app.db.models import Conversation, Lead, Location, Message, Tenant
from app.db.scope import TenantScope
from app.db.session import session_scope
from app.rag.ingest import ingest_site
from app.services.credentials import (
    KNOWN_PROVIDERS,
    delete_credential,
    list_providers,
    store_credential,
)
from app.services.retention import erase_tenant, export_tenant, purge_tenant
from app.settings import get_settings

router = APIRouter(prefix="/v1/admin", tags=["admin"])


def _auth(key: str | None) -> None:
    expected = get_settings().admin_api_key
    if not key or not secrets.compare_digest(key, expected):
        raise HTTPException(401, "bad admin key")


class TenantIn(BaseModel):
    key: str = Field(min_length=2, max_length=64)
    name: str
    pack_key: str = "generic"
    allowed_origins: list[str] = Field(default_factory=list)
    region: str = "us"
    monthly_token_budget: int = 5_000_000
    config_overrides: dict = Field(default_factory=dict)


class CrawlIn(BaseModel):
    url: str
    max_pages: int = 40
    location_key: str | None = None


class LocationIn(BaseModel):
    key: str
    name: str
    timezone: str = "UTC"
    config_overrides: dict = Field(default_factory=dict)


@router.get("/demo-key")
async def demo_key():
    """Unauthenticated, dev-only: lets /demo load a tenant without pasting a key.

    Returns 404 outside dev/test so it can never expose a live client's key.
    """
    if get_settings().env not in ("dev", "test"):
        raise HTTPException(404, "not available")
    async with session_scope() as session:
        tenant = (
            await session.execute(select(Tenant).order_by(Tenant.created_at).limit(1))
        ).scalar_one_or_none()
        if not tenant:
            raise HTTPException(404, "no tenants seeded")
        return {"public_key": tenant.public_key, "name": tenant.name, "key": tenant.key}


@router.post("/tenants")
async def create_tenant(body: TenantIn, x_admin_key: str | None = Header(default=None)):
    _auth(x_admin_key)
    if body.pack_key not in PACKS:
        raise HTTPException(400, f"unknown pack: {body.pack_key}. have: {list(PACKS)}")

    async with session_scope() as session:
        exists = (
            await session.execute(select(Tenant).where(Tenant.key == body.key))
        ).scalar_one_or_none()
        if exists:
            raise HTTPException(409, f"tenant '{body.key}' already exists")

        tenant = Tenant(
            key=body.key,
            name=body.name,
            public_key="pk_" + secrets.token_urlsafe(24),
            pack_key=body.pack_key,
            allowed_origins=body.allowed_origins,
            region=body.region,
            monthly_token_budget=body.monthly_token_budget,
            config_overrides=body.config_overrides,
        )
        session.add(tenant)
        await session.flush()
        return {
            "id": str(tenant.id),
            "key": tenant.key,
            "public_key": tenant.public_key,
            "pack": tenant.pack_key,
            "embed": (
                f'<script src="https://cdn.firstvoid.com/widget.js" '
                f'data-key="{tenant.public_key}" async></script>'
            ),
        }


@router.get("/tenants")
async def list_tenants(x_admin_key: str | None = Header(default=None)):
    _auth(x_admin_key)
    async with session_scope() as session:
        rows = (await session.execute(select(Tenant).order_by(Tenant.created_at))).scalars().all()
        return [
            {
                "id": str(t.id),
                "key": t.key,
                "name": t.name,
                "pack": t.pack_key,
                "status": t.status,
                "public_key": t.public_key,
                "tokens_used": t.tokens_used_period,
                "budget": t.monthly_token_budget,
            }
            for t in rows
        ]


@router.patch("/tenants/{tenant_key}")
async def update_tenant(
    tenant_key: str, overrides: dict, x_admin_key: str | None = Header(default=None)
):
    """Config change = a row update + a version bump. Rollback is one click."""
    _auth(x_admin_key)
    async with session_scope() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.key == tenant_key))
        ).scalar_one_or_none()
        if not tenant:
            raise HTTPException(404, "no such tenant")
        tenant.config_overrides = {**(tenant.config_overrides or {}), **overrides}
        tenant.config_version += 1
        invalidate(f"{tenant_key}:")
        return {"ok": True, "config_version": tenant.config_version}


@router.post("/tenants/{tenant_key}/locations")
async def add_location(
    tenant_key: str, body: LocationIn, x_admin_key: str | None = Header(default=None)
):
    _auth(x_admin_key)
    async with session_scope() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.key == tenant_key))
        ).scalar_one_or_none()
        if not tenant:
            raise HTTPException(404, "no such tenant")
        location = Location(
            tenant_id=tenant.id,
            key=body.key,
            name=body.name,
            timezone=body.timezone,
            config_overrides=body.config_overrides,
        )
        session.add(location)
        await session.flush()
        invalidate(f"{tenant_key}:")
        return {"id": str(location.id), "key": location.key}


@router.post("/tenants/{tenant_key}/crawl")
async def crawl_tenant(
    tenant_key: str, body: CrawlIn, x_admin_key: str | None = Header(default=None)
):
    _auth(x_admin_key)
    async with session_scope() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.key == tenant_key))
        ).scalar_one_or_none()
        if not tenant:
            raise HTTPException(404, "no such tenant")

        location_id: uuid.UUID | None = None
        if body.location_key:
            location = (
                await session.execute(
                    select(Location).where(
                        Location.tenant_id == tenant.id, Location.key == body.location_key
                    )
                )
            ).scalar_one_or_none()
            location_id = location.id if location else None

        return await ingest_site(
            session,
            tenant_id=tenant.id,
            start_url=body.url,
            location_id=location_id,
            max_pages=body.max_pages,
        )


class CredentialIn(BaseModel):
    provider: str
    payload: dict


async def _tenant_or_404(session, tenant_key: str) -> Tenant:
    tenant = (
        await session.execute(select(Tenant).where(Tenant.key == tenant_key))
    ).scalar_one_or_none()
    if not tenant:
        raise HTTPException(404, "no such tenant")
    return tenant


@router.get("/tenants/{tenant_key}/credentials")
async def get_credentials(tenant_key: str, x_admin_key: str | None = Header(default=None)):
    """Which providers are configured - never what they contain."""
    _auth(x_admin_key)
    async with session_scope() as session:
        tenant = await _tenant_or_404(session, tenant_key)
        return {
            "configured": await list_providers(session, tenant_id=tenant.id),
            "known_providers": sorted(KNOWN_PROVIDERS),
        }


@router.put("/tenants/{tenant_key}/credentials")
async def put_credential(
    tenant_key: str, body: CredentialIn, x_admin_key: str | None = Header(default=None)
):
    """Store an integration secret, Fernet-encrypted at rest.

    The response deliberately echoes nothing back: the payload arrives as plaintext
    JSON over the wire and must not be reflected into a log, a proxy trace, or a
    browser history entry.
    """
    _auth(x_admin_key)
    async with session_scope() as session:
        tenant = await _tenant_or_404(session, tenant_key)
        await store_credential(
            session, tenant_id=tenant.id, provider=body.provider, payload=body.payload
        )
        return {"ok": True, "provider": body.provider, "fields_stored": len(body.payload)}


@router.delete("/tenants/{tenant_key}/credentials/{provider}")
async def remove_credential(
    tenant_key: str, provider: str, x_admin_key: str | None = Header(default=None)
):
    _auth(x_admin_key)
    async with session_scope() as session:
        tenant = await _tenant_or_404(session, tenant_key)
        removed = await delete_credential(session, tenant_id=tenant.id, provider=provider)
        if not removed:
            raise HTTPException(404, f"no {provider} credential for this tenant")
        return {"ok": True, "provider": provider}


@router.post("/tenants/{tenant_key}/retention/preview")
async def retention_preview(tenant_key: str, x_admin_key: str | None = Header(default=None)):
    """Dry run. Deletion is irreversible - always look before purging."""
    _auth(x_admin_key)
    async with session_scope() as session:
        tenant = await _tenant_or_404(session, tenant_key)
        days, tid = tenant.retention_days, tenant.id
    return await purge_tenant(tenant_id=tid, retention_days=days, dry_run=True)


@router.post("/tenants/{tenant_key}/retention/run")
async def retention_run(tenant_key: str, x_admin_key: str | None = Header(default=None)):
    _auth(x_admin_key)
    async with session_scope() as session:
        tenant = await _tenant_or_404(session, tenant_key)
        days, tid = tenant.retention_days, tenant.id
    return await purge_tenant(tenant_id=tid, retention_days=days)


@router.get("/tenants/{tenant_key}/export")
async def export(tenant_key: str, x_admin_key: str | None = Header(default=None)):
    """Portability request. Secrets are redacted; credentials are excluded entirely."""
    _auth(x_admin_key)
    async with session_scope() as session:
        tenant = await _tenant_or_404(session, tenant_key)
        tid = tenant.id
    return await export_tenant(tenant_id=tid)


@router.delete("/tenants/{tenant_key}")
async def erase(
    tenant_key: str,
    confirm: str = "",
    x_admin_key: str | None = Header(default=None),
):
    """Erasure request: removes the tenant and everything belonging to it.

    Requires ?confirm=<tenant_key> so a mistyped curl cannot delete a live client.
    """
    _auth(x_admin_key)
    if confirm != tenant_key:
        raise HTTPException(400, f"irreversible - pass ?confirm={tenant_key} to proceed")
    async with session_scope() as session:
        tenant = await _tenant_or_404(session, tenant_key)
        tid = tenant.id
    result = await erase_tenant(tenant_id=tid)
    invalidate(f"{tenant_key}:")
    return result


@router.get("/tenants/{tenant_key}/conversations")
async def conversations(
    tenant_key: str, limit: int = 50, x_admin_key: str | None = Header(default=None)
):
    _auth(x_admin_key)
    async with session_scope() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.key == tenant_key))
        ).scalar_one_or_none()
        if not tenant:
            raise HTTPException(404, "no such tenant")

        scope = TenantScope(tenant_id=tenant.id)
        rows = (
            (
                await session.execute(
                    scope.select(Conversation).order_by(Conversation.created_at.desc()).limit(limit)
                )
            )
            .scalars()
            .all()
        )

        out = []
        for conversation in rows:
            messages = (
                (
                    await session.execute(
                        scope.select(Message)
                        .where(Message.conversation_id == conversation.id)
                        .order_by(Message.created_at)
                    )
                )
                .scalars()
                .all()
            )
            out.append(
                {
                    "id": str(conversation.id),
                    "channel": conversation.channel,
                    "status": conversation.status,
                    "intake": conversation.intake,
                    "created_at": conversation.created_at.isoformat(),
                    "messages": [
                        {
                            "role": m.role,
                            "content": m.content,
                            "trace": (m.meta or {}).get("trace", []),
                            "latency_ms": (m.meta or {}).get("latency_ms"),
                        }
                        for m in messages
                    ],
                }
            )
        return out


@router.get("/tenants/{tenant_key}/leads")
async def leads(tenant_key: str, x_admin_key: str | None = Header(default=None)):
    _auth(x_admin_key)
    async with session_scope() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.key == tenant_key))
        ).scalar_one_or_none()
        if not tenant:
            raise HTTPException(404, "no such tenant")
        scope = TenantScope(tenant_id=tenant.id)
        rows = (
            (await session.execute(scope.select(Lead).order_by(Lead.created_at.desc())))
            .scalars()
            .all()
        )
        return [
            {
                "id": str(r.id),
                "fields": r.fields,
                "status": r.status,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]


@router.get("/tenants/{tenant_key}/analytics")
async def analytics(
    tenant_key: str, days: int = 14, x_admin_key: str | None = Header(default=None)
):
    """Daily series for the console charts.

    Grouped in SQL rather than pulled into Python - at 1k conversations it makes no
    difference, but the query stays correct when the table is larger.
    """
    _auth(x_admin_key)
    days = max(1, min(days, 90))

    async with session_scope() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.key == tenant_key))
        ).scalar_one_or_none()
        if not tenant:
            raise HTTPException(404, "no such tenant")

        rows = (
            (
                await session.execute(
                    text("""
                WITH span AS (
                    SELECT generate_series(
                        (now() AT TIME ZONE 'utc')::date - CAST(:days AS int) + 1,
                        (now() AT TIME ZONE 'utc')::date,
                        interval '1 day'
                    )::date AS day
                ),
                msgs AS (
                    SELECT created_at::date AS day,
                           count(*) FILTER (WHERE role = 'assistant') AS replies,
                           coalesce(sum(tokens_in), 0)  AS tin,
                           coalesce(sum(tokens_out), 0) AS tout,
                           count(*) FILTER (
                               WHERE role = 'assistant'
                                 AND coalesce((meta ->> 'blocked')::boolean, false)
                           ) AS blocked
                    FROM messages
                    WHERE tenant_id = CAST(:tid AS uuid)
                    GROUP BY 1
                ),
                convs AS (
                    SELECT created_at::date AS day, count(*) AS n
                    FROM conversations WHERE tenant_id = CAST(:tid AS uuid) GROUP BY 1
                ),
                lds AS (
                    SELECT created_at::date AS day, count(*) AS n
                    FROM leads WHERE tenant_id = CAST(:tid AS uuid) GROUP BY 1
                )
                SELECT to_char(span.day, 'YYYY-MM-DD') AS day,
                       coalesce(convs.n, 0)      AS conversations,
                       coalesce(msgs.replies, 0) AS replies,
                       coalesce(msgs.blocked, 0) AS blocked,
                       coalesce(lds.n, 0)        AS leads,
                       coalesce(msgs.tin, 0)     AS tokens_in,
                       coalesce(msgs.tout, 0)    AS tokens_out
                FROM span
                LEFT JOIN msgs  ON msgs.day  = span.day
                LEFT JOIN convs ON convs.day = span.day
                LEFT JOIN lds   ON lds.day   = span.day
                ORDER BY span.day
                """),
                    {"tid": str(tenant.id), "days": days},
                )
            )
            .mappings()
            .all()
        )

    series = []
    for r in rows:
        tin, tout = int(r["tokens_in"]), int(r["tokens_out"])
        series.append(
            {
                **{k: int(v) if k != "day" else v for k, v in r.items()},
                # Haiku 4.5 list rates; adjust per the tenant's ModelPolicy.
                "cost_usd": round(tin / 1_000_000 * 1.0 + tout / 1_000_000 * 5.0, 5),
            }
        )
    return {"days": days, "series": series}


@router.get("/tenants/{tenant_key}/usage")
async def usage(tenant_key: str, x_admin_key: str | None = Header(default=None)):
    """Margin per tenant. Know which clients are unprofitable before renewal."""
    _auth(x_admin_key)
    async with session_scope() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.key == tenant_key))
        ).scalar_one_or_none()
        if not tenant:
            raise HTTPException(404, "no such tenant")

        totals = (
            await session.execute(
                select(
                    func.count(Message.id),
                    func.coalesce(func.sum(Message.tokens_in), 0),
                    func.coalesce(func.sum(Message.tokens_out), 0),
                ).where(Message.tenant_id == tenant.id)
            )
        ).one()
        conversation_count = (
            await session.execute(
                select(func.count(Conversation.id)).where(Conversation.tenant_id == tenant.id)
            )
        ).scalar_one()

        tokens_in, tokens_out = int(totals[1]), int(totals[2])
        # Haiku 4.5 list rates; adjust per tenant's actual ModelPolicy.
        cost = tokens_in / 1_000_000 * 1.0 + tokens_out / 1_000_000 * 5.0
        return {
            "conversations": conversation_count,
            "messages": int(totals[0]),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "est_cost_usd": round(cost, 4),
            "est_cost_per_conversation": round(cost / conversation_count, 4)
            if conversation_count
            else 0.0,
            "budget_tokens": tenant.monthly_token_budget,
            "budget_used_pct": round(
                100 * tenant.tokens_used_period / max(tenant.monthly_token_budget, 1), 2
            ),
        }
