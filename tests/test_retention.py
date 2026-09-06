"""Retention: purge transcripts, keep business records, never leak secrets."""

import uuid

from app.services.retention import (
    BATCH_SIZE,
    EXPORT_TABLES,
    MAX_BATCHES,
    SECRET_KEYS,
    redact,
)


def test_export_redacts_secrets_at_every_depth():
    payload = {
        "escalation": {
            "webhook_url": "https://hooks.example.com/x",
            "webhook_secret": "whsec_LIVE",
        },
        "integrations": [{"api_key": "sk-live-abc", "label": "keep me"}],
        "lead_webhook_secret": "another_live_secret",
    }
    out = redact(payload)

    flat = str(out)
    assert "whsec_LIVE" not in flat
    assert "sk-live-abc" not in flat
    assert "another_live_secret" not in flat
    # non-secret values survive
    assert out["escalation"]["webhook_url"] == "https://hooks.example.com/x"
    assert out["integrations"][0]["label"] == "keep me"


def test_every_known_secret_key_is_covered():
    for key in ("webhook_secret", "client_secret", "refresh_token", "api_key", "ciphertext"):
        assert key in SECRET_KEYS
        assert redact({key: "leak"})[key] == "[redacted]"


def test_redact_leaves_scalars_alone():
    assert redact("plain") == "plain"
    assert redact(42) == 42
    assert redact(None) is None


def test_export_orders_by_a_column_each_table_actually_has():
    """locations and chunks have no created_at - ordering by it would 500 at runtime."""
    import pathlib
    import re

    models = pathlib.Path("app/db/models.py").read_text()
    for table, order_col in EXPORT_TABLES.items():
        block = re.search(rf'__tablename__ = "{table}".*?(?=\nclass |\Z)', models, re.S)
        assert block, f"table {table} not found in models"
        assert order_col == "id" or f"{order_col}:" in block.group(0), (
            f"{table} has no column {order_col}"
        )


def test_credentials_are_excluded_from_export_entirely():
    assert "tenant_credentials" not in EXPORT_TABLES
    assert "chunks" not in EXPORT_TABLES


def test_purge_is_bounded():
    """An unbounded purge on a table that only grows is an outage waiting to happen."""
    assert BATCH_SIZE > 0
    assert MAX_BATCHES > 0
    assert BATCH_SIZE * MAX_BATCHES <= 250_000


async def test_zero_retention_days_means_keep_forever():
    from app.services.retention import purge_tenant

    result = await purge_tenant(tenant_id=uuid.uuid4(), retention_days=0)
    assert result["deleted"] == 0
    assert "skipped" in result


def test_business_records_detach_rather_than_cascade():
    """The whole point: a retention purge must not delete the client's leads."""
    import pathlib
    import re

    models = pathlib.Path("app/db/models.py").read_text()
    for table in ("leads", "appointments"):
        block = re.search(rf'__tablename__ = "{table}".*?(?=\nclass |\Z)', models, re.S).group(0)
        # "SET NULL" contains a space - \w+ silently fails to match it
        conv_fk = re.search(r'ForeignKey\("conversations\.id",\s*ondelete="([A-Z ]+)"\)', block)
        assert conv_fk, f"{table} has no conversations FK"
        assert conv_fk.group(1) == "SET NULL", (
            f"{table}.conversation_id is {conv_fk.group(1)} - a purge would delete "
            "the client's business records"
        )
