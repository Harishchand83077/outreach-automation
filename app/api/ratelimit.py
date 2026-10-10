"""Rate limiting, keyed on the user rather than the address.

`ARCHITECTURE_TARGET.md` §14. The prototype keyed its limiter on `request.client.host`, which
behind any proxy or PaaS is the proxy's address — so every client in the world shared one bucket
and the limit was either useless or an outage (issue C4). It was also POST-only, so the endpoint
that exports every lead as CSV was not throttled at all, and it never pruned idle entries, so the
dictionary grew for as long as the process lived.

This one keys on the authenticated user id, covers every method, and prunes as it goes. An
unauthenticated request has no user, so it is keyed on the address as a last resort — that path
exists only for endpoints that are deliberately public, which is `/health` and `/ready`.

In memory, so it resets on restart and is per-process. That is honest for one worker and one
API container. The moment there are two, this needs to move to the database or a shared store,
and the limit becomes per-process rather than global in the meantime.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class RateLimit:
    """A fixed number of requests in a sliding window."""

    limit: int
    window_seconds: float

    #: key -> timestamps of requests still inside the window
    _hits: dict[str, deque[float]] = field(default_factory=dict)
    #: When idle keys were last swept out.
    _last_sweep: float = 0.0

    def _now(self) -> float:
        return time.monotonic()

    def check(self, key: str, *, now: float | None = None) -> tuple[bool, float]:
        """Record a request. Returns (allowed, seconds until a slot frees).

        The caller records *before* knowing whether the request succeeds, deliberately: a
        limiter that only counted successes would let an attacker probe for free.
        """
        now = self._now() if now is None else now
        cutoff = now - self.window_seconds

        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= self.limit:
            self._sweep(now, keep=key)
            retry_after = max(hits[0] + self.window_seconds - now, 0.0)
            return False, retry_after

        hits.append(now)
        # Swept after recording, and never the key in hand: sweeping first would drop the entry
        # this call just created, so every request would look like a first request and the limit
        # would quietly never apply.
        self._sweep(now, keep=key)
        return True, 0.0

    def _sweep(self, now: float, *, keep: str | None = None) -> None:
        """Drop keys with nothing left in the window, so the map cannot grow for ever."""
        if now - self._last_sweep < self.window_seconds:
            return
        self._last_sweep = now
        cutoff = now - self.window_seconds
        stale = [
            k for k, hits in self._hits.items() if k != keep and (not hits or hits[-1] <= cutoff)
        ]
        for key in stale:
            del self._hits[key]

    def reset(self) -> None:
        self._hits.clear()
        self._last_sweep = 0.0
