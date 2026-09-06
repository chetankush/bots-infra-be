"""Fernet encryption for per-tenant integration credentials."""

import json
from functools import lru_cache

from cryptography.fernet import Fernet

from app.settings import get_settings


@lru_cache
def _fernet() -> Fernet:
    key = get_settings().credential_encryption_key
    if not key:
        raise RuntimeError(
            "CREDENTIAL_ENCRYPTION_KEY is unset. Generate one with:\n"
            '  python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    return Fernet(key.encode())


def encrypt(payload: dict) -> str:
    return _fernet().encrypt(json.dumps(payload).encode()).decode()


def decrypt(ciphertext: str) -> dict:
    return json.loads(_fernet().decrypt(ciphertext.encode()).decode())
