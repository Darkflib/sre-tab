"""In-process sliding-window rate limiting."""

from __future__ import annotations

from app.auth.ratelimit import SlidingWindowLimiter


class FakeClock:
    """Monotonic clock under test control — no sleeping, no wall time."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def shift(self, seconds: float) -> None:
        self.now += seconds


def test_allows_up_to_the_limit_then_refuses() -> None:
    limiter = SlidingWindowLimiter(limit=3, window_seconds=60)
    assert [limiter.hit("10.0.0.1") for _ in range(4)] == [True, True, True, False]


def test_keys_are_independent() -> None:
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
    assert limiter.hit("10.0.0.1") is True
    assert limiter.hit("10.0.0.1") is False
    assert limiter.hit("10.0.0.2") is True


def test_window_slides() -> None:
    clock = FakeClock()
    limiter = SlidingWindowLimiter(limit=2, window_seconds=60, clock=clock)
    assert limiter.hit("ip") is True
    assert limiter.hit("ip") is True
    assert limiter.hit("ip") is False
    clock.shift(61)
    assert limiter.hit("ip") is True


def test_is_limited_does_not_consume_budget() -> None:
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
    assert limiter.is_limited("ip") is False
    assert limiter.is_limited("ip") is False
    assert limiter.hit("ip") is True
    assert limiter.is_limited("ip") is True


def test_reset_restores_full_budget() -> None:
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
    assert limiter.hit("ip") is True
    assert limiter.hit("ip") is False
    limiter.reset()
    assert limiter.hit("ip") is True
