"""App shell smoke tests: boot, healthz, headers, request IDs."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.health import probes


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


def test_security_headers_present(client: TestClient) -> None:
    response = client.get("/api/v1/healthz")
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_request_id_generated_and_incoming_honoured(client: TestClient) -> None:
    generated = client.get("/api/v1/healthz").headers["X-Request-ID"]
    assert len(generated) == 32

    echoed = client.get(
        "/api/v1/healthz", headers={"X-Request-ID": "proxy-supplied-id-01"}
    ).headers["X-Request-ID"]
    assert echoed == "proxy-supplied-id-01"
