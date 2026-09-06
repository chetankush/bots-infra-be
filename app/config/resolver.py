"""Deep-merge the config layers into a validated AgentConfig.

    platform defaults -> pack -> tenant -> location -> channel profile

Resolved configs are cached in-process with a TTL; every instance stays disposable.
"""

import time
from copy import deepcopy
from typing import Any

from app.config.schema import CHANNEL_PROFILES, AgentConfig, Channel

_CACHE: dict[str, tuple[float, AgentConfig]] = {}
_TTL_SECONDS = 60.0


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursive dict merge. Lists replace wholesale - a tenant that overrides
    `prohibitions` means exactly that list, not an append."""
    out = deepcopy(base)
    for key, value in (override or {}).items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def resolve(
    *,
    pack: dict[str, Any] | None,
    tenant: dict[str, Any] | None,
    location: dict[str, Any] | None,
    channel: Channel = "web",
) -> AgentConfig:
    merged: dict[str, Any] = {}
    for layer in (pack, tenant, location):
        merged = deep_merge(merged, layer or {})
    merged = deep_merge(merged, {"channel": CHANNEL_PROFILES.get(channel, {})})
    return AgentConfig.model_validate(merged)


def cache_key(tenant_key: str, location_key: str | None, channel: str, version: int) -> str:
    return f"{tenant_key}:{location_key or '-'}:{channel}:{version}"


def get_cached(key: str) -> AgentConfig | None:
    hit = _CACHE.get(key)
    if not hit:
        return None
    ts, cfg = hit
    if time.monotonic() - ts > _TTL_SECONDS:
        _CACHE.pop(key, None)
        return None
    return cfg


def put_cached(key: str, cfg: AgentConfig) -> None:
    _CACHE[key] = (time.monotonic(), cfg)


def invalidate(prefix: str) -> None:
    for k in [k for k in _CACHE if k.startswith(prefix)]:
        _CACHE.pop(k, None)
