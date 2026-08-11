"""Client-side rate limiting.

Kalshi meters reads and writes separately and per tier. Being throttled by the
exchange is worse than waiting a few milliseconds locally: a 429 in the middle
of placing an exit leaves a position open with no order protecting it.

A token bucket rather than a fixed sleep, so bursts are allowed — placing an
entry and its exit back to back is exactly the pattern that matters — while the
sustained rate stays inside the limit.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Tier:
    """Requests per second allowed for reads and writes."""

    name: str
    reads_per_s: float
    writes_per_s: float


# Conservative defaults: the published entry-level allowance, minus headroom,
# because the cost of guessing high is a 429 at the worst possible moment.
TIERS: dict[str, Tier] = {
    "basic": Tier("basic", reads_per_s=8, writes_per_s=4),
    "advanced": Tier("advanced", reads_per_s=30, writes_per_s=20),
    "premier": Tier("premier", reads_per_s=100, writes_per_s=100),
    "prime": Tier("prime", reads_per_s=400, writes_per_s=400),
}


class TokenBucket:
    def __init__(self, rate_per_s: float, burst: float | None = None) -> None:
        self.rate = max(0.1, rate_per_s)
        self.capacity = burst if burst is not None else max(1.0, rate_per_s)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()
        self.waits = 0
        self.waited_s = 0.0

    async def take(self, tokens: float = 1.0) -> None:
        """Wait until `tokens` are available, then consume them."""
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._updated) * self.rate
                )
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                delay = (tokens - self._tokens) / self.rate
                self.waits += 1
                self.waited_s += delay
                await asyncio.sleep(delay)


class Throttle:
    """Separate buckets for reads and writes, matching how Kalshi meters."""

    def __init__(self, tier: str | Tier = "basic") -> None:
        resolved = tier if isinstance(tier, Tier) else TIERS.get(str(tier), TIERS["basic"])
        self.tier = resolved
        self.reads = TokenBucket(resolved.reads_per_s)
        self.writes = TokenBucket(resolved.writes_per_s)

    async def acquire(self, method: str) -> None:
        if method.upper() in {"GET", "HEAD"}:
            await self.reads.take()
        else:
            await self.writes.take()

    @property
    def stats(self) -> dict[str, float]:
        return {
            "tier": self.tier.name,
            "read_waits": self.reads.waits,
            "write_waits": self.writes.waits,
            "waited_s": round(self.reads.waited_s + self.writes.waited_s, 2),
        }
