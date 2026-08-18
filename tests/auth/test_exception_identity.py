"""Raised exceptions are constructed per raise, never shared instances.

A module-global ``HTTPException`` raised on every failure is the shape
this guards against. Python appends a frame to ``__traceback__`` on each
``raise``, and a module global is never collected, so the object grows
one traceback entry per request for the life of the process — pinning
every ``Request``, raw token, and ``Session`` those frames hold. Measured
at 32,719 bytes per unauthenticated request against a unit capped at
``MemoryMax=768M``, on routes with no credential requirement and no rate
limiter.

Both checks below were failed on purpose against the original code
before being committed, per AGENTS.md: a gate nobody has seen fail is not
known to be a gate.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

from fastapi.testclient import TestClient

from app.api.v1.auth import callback_failure_limiter, start_limiter


def _module_level_exceptions() -> Iterator[tuple[str, BaseException]]:
    """Every exception *instance* held at module scope anywhere in ``app``.

    Deliberately name-agnostic. The invariant is "no shared exception
    instances", not "these two names are absent", so renaming or moving
    the offender does not slip past this.
    """
    for module_name in sorted(sys.modules):
        if module_name != "app" and not module_name.startswith("app."):
            continue
        module = sys.modules[module_name]
        for attribute, value in vars(module).items():
            if isinstance(value, BaseException):
                yield f"{module_name}.{attribute}", value


def test_no_exception_instances_are_held_at_module_scope() -> None:
    held = [name for name, _ in _module_level_exceptions()]
    assert held == [], (
        "module-level exception instances accumulate a traceback frame per "
        f"raise and are never collected: {held}. Construct a fresh one at "
        "each raise site instead."
    )


def test_repeated_unauthenticated_requests_do_not_accumulate_tracebacks(
    client: TestClient,
) -> None:
    """The end-to-end claim, not just the shape of the source."""
    for index in range(25):
        response = client.get("/api/v1/me", cookies={"sre_tab_session": f"forged-token-{index}"})
        assert response.status_code == 401

    retained = [
        (name, exc) for name, exc in _module_level_exceptions() if exc.__traceback__ is not None
    ]
    assert retained == [], f"tracebacks retained after 25 unauthenticated requests: {retained}"


def test_repeated_throttled_requests_do_not_accumulate_tracebacks(client: TestClient) -> None:
    """The 429 path matters more than the 401 one.

    ``github_callback`` raises it at the top of the function, where
    ``code`` and ``state`` are already bound, so a shared instance pins
    real OAuth codes in frame locals — the thing AGENTS.md's logging rule
    forbids, arriving by another route.
    """
    # Spend the callback failure budget: the declined branch counts every
    # credential-free callback against it.
    for _ in range(callback_failure_limiter.limit):
        client.get(
            "/api/v1/auth/github/callback",
            params={"error": "access_denied"},
            follow_redirects=False,
        )

    response = client.get(
        "/api/v1/auth/github/callback",
        params={"code": "oauth-code-must-never-be-retained", "state": "s"},
        follow_redirects=False,
    )
    assert response.status_code == 429

    # And the throttled start path, which shares the same factory.
    for _ in range(start_limiter.limit + 1):
        start = client.get("/api/v1/auth/github/start", follow_redirects=False)
    assert start.status_code == 429

    retained = [
        (name, exc) for name, exc in _module_level_exceptions() if exc.__traceback__ is not None
    ]
    assert retained == [], f"tracebacks retained after throttled requests: {retained}"


def test_each_call_builds_a_distinct_exception() -> None:
    """Imported inside the test on purpose.

    A module-level import of these two names turns a reversion into a
    *collection* error for the whole file, which takes the two checks
    above down with it — and those are the ones that say what is wrong.
    """
    from app.api.deps import _unauthenticated
    from app.api.v1.auth import _too_many_requests

    assert _unauthenticated() is not _unauthenticated()
    assert _too_many_requests() is not _too_many_requests()
