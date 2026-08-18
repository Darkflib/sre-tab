"""App shell smoke tests: boot, healthz, headers, request IDs."""

from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from app.health import ProbeResult, probes
from app.main import BoundedCheck


def test_app_boots_and_healthz_is_ok(client: TestClient) -> None:
    response = client.get("/api/v1/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["live"] is True
    assert body["ready"] is True
    assert body["liveness"]["app"]["ok"] is True
    assert body["readiness"]["database"]["ok"] is True


def test_healthz_distinguishes_readiness_failure(client: TestClient) -> None:
    probes.register_readiness("smoke_failing", lambda: False)
    try:
        response = client.get("/api/v1/healthz")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert body["live"] is True
        assert body["ready"] is False
        assert body["readiness"]["smoke_failing"]["ok"] is False
    finally:
        # Registration replaces by name; restore a passing check.
        probes.register_readiness("smoke_failing", lambda: True)


def test_bounded_check_passes_the_underlying_result_through() -> None:
    check = BoundedCheck(lambda: ProbeResult(ok=True, detail="fine"), timeout=5.0)
    assert check() == ProbeResult(ok=True, detail="fine")


def test_bounded_check_gives_up_on_a_frozen_dependency() -> None:
    """A frozen database blocks in recv with nothing to time it out; the probe
    has to impose the deadline itself or /healthz simply stops answering."""
    release = threading.Event()

    def frozen() -> ProbeResult:
        release.wait(30)
        return ProbeResult(ok=True)

    try:
        check = BoundedCheck(frozen, timeout=0.05)
        started = time.monotonic()
        result = check()
        elapsed = time.monotonic() - started

        assert result.ok is False
        assert "timeout" in (result.detail or "")
        assert elapsed < 5.0
    finally:
        release.set()


def test_bounded_check_coalesces_callers_while_stalled() -> None:
    """Polling a frozen dependency must not open a connection per request."""
    release = threading.Event()
    lock = threading.Lock()
    starts = 0

    def frozen() -> ProbeResult:
        nonlocal starts
        with lock:
            starts += 1
        release.wait(30)
        return ProbeResult(ok=True)

    try:
        check = BoundedCheck(frozen, timeout=0.05)
        for _ in range(5):
            assert check().ok is False
        with lock:
            assert starts == 1
    finally:
        release.set()


def test_healthz_reports_a_timed_out_readiness_check(client: TestClient) -> None:
    release = threading.Event()

    def frozen() -> ProbeResult:
        release.wait(30)
        return ProbeResult(ok=True)

    probes.register_readiness("smoke_frozen", BoundedCheck(frozen, timeout=0.05))
    try:
        response = client.get("/api/v1/healthz")
        assert response.status_code == 503
        assert response.json()["readiness"]["smoke_frozen"]["ok"] is False
        assert "timeout" in response.json()["readiness"]["smoke_frozen"]["detail"]
    finally:
        release.set()
        probes.register_readiness("smoke_frozen", lambda: True)


def test_security_headers_present(client: TestClient) -> None:
    response = client.get("/api/v1/healthz")
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["X-Frame-Options"] == "DENY"
    # Not `includeSubDomains` — see the note in app/middleware.py. Asserted
    # exactly rather than by substring so that adding it becomes a deliberate
    # change to the documented topology and not a quiet one.
    assert response.headers["Strict-Transport-Security"] == "max-age=31536000"


def test_request_id_generated_and_incoming_honoured(client: TestClient) -> None:
    generated = client.get("/api/v1/healthz").headers["X-Request-ID"]
    assert len(generated) == 32

    echoed = client.get(
        "/api/v1/healthz", headers={"X-Request-ID": "proxy-supplied-id-01"}
    ).headers["X-Request-ID"]
    assert echoed == "proxy-supplied-id-01"
