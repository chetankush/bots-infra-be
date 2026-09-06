"""Credential storage: encrypted at rest, tenant-scoped, never leaked."""

import uuid

import pytest
from cryptography.fernet import InvalidToken

from app.db.crypto import decrypt, encrypt
from app.db.models import TenantCredential
from app.db.scope import TenantScope


def test_payload_is_encrypted_at_rest():
    payload = {"refresh_token": "1//0gSuperSecretValue", "client_secret": "GOCSPX-abc"}
    ciphertext = encrypt(payload)

    # the secret must not be recoverable from the stored string
    assert "1//0gSuperSecretValue" not in ciphertext
    assert "GOCSPX-abc" not in ciphertext
    assert decrypt(ciphertext) == payload


def test_ciphertext_differs_per_write_but_decrypts_the_same():
    """Fernet includes a random IV - identical payloads must not produce identical rows."""
    payload = {"token": "same"}
    a, b = encrypt(payload), encrypt(payload)
    assert a != b
    assert decrypt(a) == decrypt(b) == payload


def test_credential_query_is_tenant_scoped():
    scope = TenantScope(tenant_id=uuid.uuid4())
    sql = str(scope.select(TenantCredential))
    assert "tenant_id" in sql and "WHERE" in sql


def test_tampered_ciphertext_is_rejected_not_silently_decoded():
    ciphertext = encrypt({"token": "abc"})
    tampered = ciphertext[:-4] + ("AAAA" if not ciphertext.endswith("AAAA") else "BBBB")
    with pytest.raises(InvalidToken):
        decrypt(tampered)


def test_known_providers_cover_the_planned_integrations():
    from app.services.credentials import KNOWN_PROVIDERS

    for needed in ("google_calendar", "twilio", "resend"):
        assert needed in KNOWN_PROVIDERS
