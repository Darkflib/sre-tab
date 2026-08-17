from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ProbeStatus(BaseModel):
    ok: bool
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    live: bool
    ready: bool
    liveness: dict[str, ProbeStatus]
    readiness: dict[str, ProbeStatus]
