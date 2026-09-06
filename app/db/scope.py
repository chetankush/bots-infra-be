"""Tenant scoping.

Every read and write goes through here. A forgotten WHERE clause is the failure
that ends an agency - so the tenant predicate is injected, not remembered.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Base

T = TypeVar("T", bound=Base)


class TenantIsolationError(RuntimeError):
    pass


@dataclass(frozen=True)
class TenantScope:
    tenant_id: uuid.UUID
    location_id: uuid.UUID | None = None

    def select(self, model: type[T]) -> Select[tuple[T]]:
        if not hasattr(model, "tenant_id"):
            raise TenantIsolationError(f"{model.__name__} has no tenant_id; cannot scope it")
        return select(model).where(model.tenant_id == self.tenant_id)

    def stamp(self, obj: T) -> T:
        """Force tenant_id on a new row regardless of what the caller passed."""
        if not hasattr(obj, "tenant_id"):
            raise TenantIsolationError(f"{type(obj).__name__} has no tenant_id; cannot scope it")
        obj.tenant_id = self.tenant_id
        return obj

    async def add(self, session: AsyncSession, obj: T) -> T:
        session.add(self.stamp(obj))
        await session.flush()
        return obj

    async def get(self, session: AsyncSession, model: type[T], pk: Any) -> T | None:
        row = await session.get(model, pk)
        if row is None:
            return None
        if getattr(row, "tenant_id", None) != self.tenant_id:
            raise TenantIsolationError(
                f"cross-tenant access blocked: {model.__name__} {pk} is not owned by "
                f"tenant {self.tenant_id}"
            )
        return row
