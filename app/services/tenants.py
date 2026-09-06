"""Tenant resolution, origin allowlist, and budget enforcement.

A public widget is an open LLM endpoint on the internet. The key alone is never enough -
the origin is checked server-side, and spend is bounded per tenant.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.packs import PACKS
from app.config.resolver import cache_key, get_cached, put_cached, resolve
from app.config.schema import CONSENT_REGIONS, AgentConfig, Channel
from app.db.models import Conversation, Location, Tenant


class TenantError(Exception):
    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


@dataclass
class ResolvedTenant:
    tenant: Tenant
    location: Location | None
    config: AgentConfig

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.tenant.id

    @property
    def location_id(self) -> uuid.UUID | None:
        return self.location.id if self.location else None

    def consent_required(self) -> bool:
        """Server-side, and never influenced by the request body.

        An explicit config value wins; otherwise the tenant's region decides. A tenant
        that omits the key in an EU region still gets the gate.
        """
        explicit = self.config.consent.required
        if explicit is not None:
            return bool(explicit)
        return (self.tenant.region or "us").lower() in CONSENT_REGIONS


def _origin_allowed(origin: str | None, allowed: list[str]) -> bool:
    if not allowed:
        return True  # unconfigured tenant (dev); lock this down before going live
    if not origin:
        return False
    host = (urlparse(origin).netloc or origin).lower().removeprefix("www.")
    for entry in allowed:
        candidate = (urlparse(entry).netloc or entry).lower().removeprefix("www.")
        if candidate.startswith("*."):
            if host == candidate[2:] or host.endswith("." + candidate[2:]):
                return True
        elif host == candidate:
            return True
    return False


async def resolve_tenant(
    session: AsyncSession,
    *,
    public_key: str,
    origin: str | None,
    channel: Channel = "web",
    location_key: str | None = None,
) -> ResolvedTenant:
    tenant = (
        await session.execute(select(Tenant).where(Tenant.public_key == public_key))
    ).scalar_one_or_none()

    if tenant is None:
        raise TenantError("unknown key", 404)
    if tenant.status == "paused":
        raise TenantError("this assistant is paused", 403)
    if channel == "web" and not _origin_allowed(origin, tenant.allowed_origins or []):
        raise TenantError("origin not allowed", 403)

    location = None
    if location_key:
        location = (
            await session.execute(
                select(Location).where(
                    Location.tenant_id == tenant.id, Location.key == location_key
                )
            )
        ).scalar_one_or_none()

    key = cache_key(tenant.key, location_key, channel, tenant.config_version)
    cfg = get_cached(key)
    if cfg is None:
        cfg = resolve(
            pack=PACKS.get(tenant.pack_key, PACKS["generic"]),
            tenant={"business_name": tenant.name, **(tenant.config_overrides or {})},
            location=(location.config_overrides if location else None),
            channel=channel,
        )
        put_cached(key, cfg)

    return ResolvedTenant(tenant=tenant, location=location, config=cfg)


def check_budget(tenant: Tenant) -> None:
    """Cost is the only unbounded axis. Bound it before it bounds you."""
    now = datetime.now(UTC)
    started = tenant.period_started_at
    if started and started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    if started and now - started > timedelta(days=31):
        tenant.tokens_used_period = 0
        tenant.cost_usd_period = 0.0
        tenant.period_started_at = now
        if tenant.status == "over_budget":
            tenant.status = "active"
        return

    if tenant.tokens_used_period >= tenant.monthly_token_budget:
        tenant.status = "over_budget"
        raise TenantError("monthly usage limit reached", 429)


async def get_or_create_conversation(
    session: AsyncSession,
    *,
    resolved: ResolvedTenant,
    session_id: str,
    channel: str,
    locale: str,
    consent: bool,
    meta: dict,
) -> Conversation:
    conversation = (
        await session.execute(
            select(Conversation).where(
                Conversation.tenant_id == resolved.tenant_id,
                Conversation.session_id == session_id,
            )
        )
    ).scalar_one_or_none()

    if conversation:
        return conversation

    conversation = Conversation(
        tenant_id=resolved.tenant_id,
        location_id=resolved.location_id,
        session_id=session_id,
        channel=channel,
        locale=locale,
        consent=consent,
        visitor_meta=meta,
    )
    session.add(conversation)
    await session.flush()
    return conversation
