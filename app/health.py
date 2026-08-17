"""Liveness/readiness probe registry.

Components register named checks here instead of editing the health
endpoint — e.g. the Phase 1 ingest agent does::

    from app.health import probes
    probes.register_readiness("scheduler", check_fn)

Re-registering a name replaces the previous check (idempotent, so app
factories and tests can register freely). Checks return ``bool`` or a
:class:`ProbeResult`; exceptions are caught and reported as failures.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

ProbeCheck = Callable[[], "ProbeResult | bool"]


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str | None = None


@dataclass(frozen=True)
class HealthReport:
    live: bool
    ready: bool
    liveness: dict[str, ProbeResult]
    readiness: dict[str, ProbeResult]

    @property
    def status(self) -> str:
        return "ok" if self.live and self.ready else "degraded"


def _run_check(check: ProbeCheck) -> ProbeResult:
    try:
        result = check()
    except Exception as exc:
        return ProbeResult(ok=False, detail=f"{type(exc).__name__}: {exc}")
    if isinstance(result, bool):
        return ProbeResult(ok=result)
    return result


class ProbeRegistry:
    def __init__(self) -> None:
        self._liveness: dict[str, ProbeCheck] = {}
        self._readiness: dict[str, ProbeCheck] = {}

    def register_liveness(self, name: str, check: ProbeCheck) -> None:
        self._liveness[name] = check

    def register_readiness(self, name: str, check: ProbeCheck) -> None:
        self._readiness[name] = check

    def run(self) -> HealthReport:
        liveness = {name: _run_check(check) for name, check in self._liveness.items()}
        readiness = {name: _run_check(check) for name, check in self._readiness.items()}
        return HealthReport(
            live=all(result.ok for result in liveness.values()),
            ready=all(result.ok for result in readiness.values()),
            liveness=liveness,
            readiness=readiness,
        )


# Process-wide registry: importers register against this instance.
probes = ProbeRegistry()
