"""Token-bucket rate limiting.

A public widget is an open LLM endpoint on the internet. Without this, one scraper
or one runaway loop on a client's page turns a $20 month into a $400 one - and the
tenant budget only catches it after the tokens are already spent.

In-process and lock-free: a bucket lookup is a dict hit, so it costs nothing on the
hot path. Limits are therefore per-instance - with two boxes behind a load balancer
the effective ceiling doubles. That is fine at this scale; move to Postgres or Redis
counters when it isn't.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(self, rate_per_min: float, burst: int) -> None:
        self.rate = rate_per_min / 60.0
        self.burst = float(burst)
        self._buckets: dict[str, Bucket] = {}
        self._last_sweep = time.monotonic()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._buckets.get(key)

        if bucket is None:
            self._buckets[key] = Bucket(tokens=self.burst - 1.0, updated=now)
            self._sweep(now)
            return True

        bucket.tokens = min(self.burst, bucket.tokens + (now - bucket.updated) * self.rate)
        bucket.updated = now

        if bucket.tokens < 1.0:
            return False
        bucket.tokens -= 1.0
        return True

    def _sweep(self, now: float) -> None:
        """Drop idle buckets so a scraper cycling IPs can't grow this unboundedly."""
        if now - self._last_sweep < 300:
            return
        self._last_sweep = now
        cutoff = now - 900
        for key in [k for k, b in self._buckets.items() if b.updated < cutoff]:
            self._buckets.pop(key, None)


# Per visitor session: a human sends a handful of messages a minute.
per_session = RateLimiter(rate_per_min=12, burst=6)
# Per source IP: catches one machine cycling session ids.
per_ip = RateLimiter(rate_per_min=30, burst=15)
# Per tenant: a blast radius cap, well above any real site's traffic.
per_tenant = RateLimiter(rate_per_min=300, burst=100)
