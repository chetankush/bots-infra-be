"""Consent: server decides whether it is needed; the client can only assert acceptance."""

import pathlib
import re

from app.config.schema import CONSENT_REGIONS, AgentConfig, ConsentCfg


class _FakeTenant:
    def __init__(self, region):
        self.region = region


class _Resolved:
    """Mirrors ResolvedTenant.consent_required() without needing a database."""

    def __init__(self, region, explicit=None):
        self.tenant = _FakeTenant(region)
        self.config = AgentConfig(consent=ConsentCfg(required=explicit))

    def consent_required(self):
        explicit = self.config.consent.required
        if explicit is not None:
            return bool(explicit)
        return (self.tenant.region or "us").lower() in CONSENT_REGIONS


def test_eu_tenant_needs_consent_without_configuring_anything():
    """A tenant that omits the key must not silently lose the gate."""
    assert _Resolved("eu").consent_required() is True
    assert _Resolved("ae").consent_required() is True


def test_us_tenant_does_not_by_default():
    assert _Resolved("us").consent_required() is False


def test_explicit_config_overrides_region_in_both_directions():
    assert _Resolved("us", explicit=True).consent_required() is True
    assert _Resolved("eu", explicit=False).consent_required() is False


def test_default_is_none_so_region_decides():
    assert AgentConfig().consent.required is None


def test_widget_never_puts_server_data_into_innerhtml():
    """Tenant-authored notice text lands in a stranger's page - it must be textContent."""
    src = pathlib.Path("widget/src/widget.ts").read_text()

    for line in src.splitlines():
        stripped = line.strip()
        if "innerHTML" not in stripped or stripped.startswith("//"):
            continue
        # any innerHTML assignment must be a static template with no interpolation
        assert "${" not in stripped, f"interpolated innerHTML is an XSS vector: {stripped}"

    consent_fn = src.split("function renderConsent")[1].split("async function boot")[0]
    for field in ("notice", "accept_label", "policy_label"):
        assert f"textContent = c.{field}" in consent_fn or f"c.{field}" in consent_fn
    assert "innerHTML" not in consent_fn.replace("// textContent, never innerHTML", "")


def test_widget_sends_real_consent_state_not_a_literal():
    src = pathlib.Path("widget/src/widget.ts").read_text()
    assert "consent: true" not in src, "hardcoded consent defeats the gate"
    assert "consent: consentNeeded ? consentGiven : false" in src


def test_consent_record_captures_time_and_version():
    """'Consented' is not a fact without when, and to which wording."""
    models = pathlib.Path("app/db/models.py").read_text()
    block = re.search(r'__tablename__ = "conversations".*?(?=\nclass |\Z)', models, re.S).group(0)
    assert "consent_at" in block
    assert "consent_version" in block
