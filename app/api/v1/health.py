"""Health endpoint — Phase 0 property, frozen.

Never edited by Phase 1 agents: new checks are registered on the probe
registry (``app.health.probes``), which this endpoint reports.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.v1.schemas import HealthResponse, ProbeStatus
from app.health import probes

router = APIRouter(tags=["health"])


@router.get(
    "/healthz",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse, "description": "Live but not ready"}},
)
def healthz(response: Response) -> HealthResponse:
    report = probes.run()
    if not (report.live and report.ready):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if report.live and report.ready else "degraded",
        live=report.live,
        ready=report.ready,
        liveness={n: ProbeStatus(ok=r.ok, detail=r.detail) for n, r in report.liveness.items()},
        readiness={n: ProbeStatus(ok=r.ok, detail=r.detail) for n, r in report.readiness.items()},
    )
