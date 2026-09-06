"""Cross-tenant leakage is the failure that ends an agency."""

import uuid

import pytest

from app.db.models import Conversation, Lead
from app.db.scope import TenantIsolationError, TenantScope


def test_scoped_select_injects_tenant_predicate():
    scope = TenantScope(tenant_id=uuid.uuid4())
    sql = str(scope.select(Conversation))
    assert "tenant_id" in sql and "WHERE" in sql


def test_stamp_overrides_a_forged_tenant_id():
    real = uuid.uuid4()
    scope = TenantScope(tenant_id=real)
    forged = Lead(tenant_id=uuid.uuid4(), conversation_id=uuid.uuid4(), fields={})
    scope.stamp(forged)
    assert forged.tenant_id == real


def test_unscopeable_model_is_rejected_not_silently_allowed():
    from app.db.models import Pack

    scope = TenantScope(tenant_id=uuid.uuid4())
    with pytest.raises(TenantIsolationError):
        scope.select(Pack)
