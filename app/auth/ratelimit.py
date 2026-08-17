"""In-process sliding-window rate limiting.

Stdlib only and single-instance by contract (AGENTS.md): v1 runs one
process, and no rate-limit dependency is provided. Two limiters are wired
up in :mod:`app.api.v1.auth` — OAuth initiation, and OAuth callback
*failures* — both keyed by client IP, as the PRD requires.

Callback failures rather than callback requests: a successful sign-in is
not something to throttle, but a stream of failures is either an attacker
grinding at ``state``/``code`` or a misconfiguration worth slowing down.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

# Bound on distinct keys tracked, so a spray of forged source addresses
# cannot grow the process without limit.
_MAX_KEYS = 8192


class SlidingWindowLimiter:
    """Allow at most ``limit`` events per ``window_seconds`` per key.

    The clock is ``time.monotonic`` so a system clock correction cannot
    hand an attacker a fresh budget, and injectable so tests can advance
    it without sleeping.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @property
    def limit(self) -> int:
        return self._limit

    def hit(self, key: str) -> bool:
        """Record an event. False once the key is over its limit."""
        now = self._clock()
        with self._lock:
            events = self._prune_locked(key, now)
            if len(events) >= self._limit:
                return False
            events.append(now)
            return True

    def is_limited(self, key: str) -> bool:
        """Read-only check; does not consume budget."""
        now = self._clock()
        with self._lock:
            return len(self._prune_locked(key, now)) >= self._limit

    def reset(self) -> None:
        with self._lock:
            self._events.clear()

    def _prune_locked(self, key: str, now: float) -> deque[float]:
        cutoff = now - self._window
        for tracked in list(self._events):
            events = self._events[tracked]
            while events and events[0] <= cutoff:
                events.popleft()
            if not events and tracked != key:
                del self._events[tracked]
        while len(self._events) >= _MAX_KEYS:
            # Evict least-recently-active first. Never the key being
            # checked, or a spray of forged addresses would clear the
            # budget of the address actually being limited.
            stale = min(
                (tracked for tracked in self._events if tracked != key),
                key=lambda tracked: self._events[tracked][-1],
                default=None,
            )
            if stale is None:
                break
            del self._events[stale]
        return self._events.setdefault(key, deque())
