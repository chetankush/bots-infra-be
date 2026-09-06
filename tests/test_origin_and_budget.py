"""A public widget is an open endpoint. The key alone is never enough."""

from datetime import UTC, datetime, timedelta

import pytest

from app.db.models import Tenant
from app.services.tenants import TenantError, _origin_allowed, check_budget


def test_origin_allowlist():
    allowed = ["https://northsidemotors.com", "*.dealer.co"]
    assert _origin_allowed("https://northsidemotors.com", allowed)
    assert _origin_allowed("https://www.northsidemotors.com", allowed)
    assert _origin_allowed("https://pune.dealer.co", allowed)
    assert not _origin_allowed("https://evil.com", allowed)
    assert not _origin_allowed(None, allowed)
    assert not _origin_allowed("https://northsidemotors.com.evil.com", allowed)


def test_empty_allowlist_is_permissive_for_dev_only():
    assert _origin_allowed("https://anything.com", [])


def _tenant(**kw) -> Tenant:
    base = dict(
        key="t",
        name="T",
        public_key="pk_x",
        monthly_token_budget=1000,
        tokens_used_period=0,
        period_started_at=datetime.now(UTC),
        status="active",
    )
    return Tenant(**{**base, **kw})


def test_budget_blocks_when_exhausted():
    t = _tenant(tokens_used_period=1000)
    with pytest.raises(TenantError) as exc:
        check_budget(t)
    assert exc.value.status == 429
    assert t.status == "over_budget"


def test_budget_allows_under_limit():
    check_budget(_tenant(tokens_used_period=999))


def test_budget_period_rolls_over_and_reinstates():
    t = _tenant(
        tokens_used_period=5000,
        status="over_budget",
        period_started_at=datetime.now(UTC) - timedelta(days=32),
    )
    check_budget(t)
    assert t.tokens_used_period == 0
    assert t.status == "active"
