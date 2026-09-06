"""Per-tenant integration credentials.

`TenantCredential` has existed since the first migration and nothing has ever written
to it - every integration spec tripped over the same missing half. This is it.

Rules:
- Ciphertext never leaves this module. Callers get a decrypted dict or None.
- Every query is tenant-scoped through TenantScope, so the predicate is injected
  rather than remembered.
- A decrypt failure (a rotated CREDENTIAL_ENCRYPTION_KEY) returns None rather than
  raising, so an integration degrades instead of crashing a visitor's turn.
- Nothing here ever logs a payload, a key name, or any fragment of a secret.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crypto import decrypt, encrypt
from app.db.models import TenantCredential
from app.db.scope import TenantScope
from app.logging import get_logger

log = get_logger("credentials")

# Providers the engine knows how to use. Storing an unknown provider is allowed
# (forward compatibility) but is logged, because it is usually a typo.
KNOWN_PROVIDERS = frozenset({"google_calendar", "twilio", "resend", "calendly", "hubspot", "slack"})


async def load_credential(
    session: AsyncSession, *, tenant_id: uuid.UUID, provider: str
) -> dict | None:
    row = (
        await session.execute(
            TenantScope(tenant_id=tenant_id)
            .select(TenantCredential)
            .where(TenantCredential.provider == provider)
        )
    ).scalar_one_or_none()

    if row is None:
        return None

    try:
        payload = decrypt(row.ciphertext)
    except Exception:
        # Almost always a rotated encryption key. Never log the ciphertext.
        log.warning("credential_undecryptable", provider=provider, tenant_id=str(tenant_id))
        return None

    return payload if isinstance(payload, dict) else None


async def store_credential(
    session: AsyncSession, *, tenant_id: uuid.UUID, provider: str, payload: dict
) -> None:
    """Upsert, honouring the (tenant_id, provider) unique constraint."""
    if not isinstance(payload, dict) or not payload:
        raise ValueError("credential payload must be a non-empty object")
    if provider not in KNOWN_PROVIDERS:
        log.warning("credential_unknown_provider", provider=provider)

    row = (
        await session.execute(
            TenantScope(tenant_id=tenant_id)
            .select(TenantCredential)
            .where(TenantCredential.provider == provider)
        )
    ).scalar_one_or_none()

    ciphertext = encrypt(payload)
    if row is None:
        session.add(TenantCredential(tenant_id=tenant_id, provider=provider, ciphertext=ciphertext))
    else:
        row.ciphertext = ciphertext

    log.info("credential_stored", provider=provider, tenant_id=str(tenant_id))


async def delete_credential(session: AsyncSession, *, tenant_id: uuid.UUID, provider: str) -> bool:
    row = (
        await session.execute(
            TenantScope(tenant_id=tenant_id)
            .select(TenantCredential)
            .where(TenantCredential.provider == provider)
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    await session.delete(row)
    log.info("credential_deleted", provider=provider, tenant_id=str(tenant_id))
    return True


async def list_providers(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[dict]:
    """Metadata only - which providers are configured, never what they contain."""
    rows = (
        await session.execute(
            select(TenantCredential.provider, TenantCredential.created_at).where(
                TenantCredential.tenant_id == tenant_id
            )
        )
    ).all()
    return [{"provider": p, "created_at": c.isoformat()} for p, c in rows]
