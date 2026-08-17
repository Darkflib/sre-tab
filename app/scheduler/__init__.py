"""In-process scheduling for feed refresh and retention pruning.

:func:`app.main.create_app` calls :func:`install_scheduler` once
``application.state`` is populated; that is what starts the scheduler and
puts a ``scheduler`` readiness probe in ``/api/v1/healthz``.

``SOURCE_REFRESH_ENABLED=false`` is the test and maintenance posture:
nothing is scheduled, no thread starts, and the probe reports
ready-and-disabled.
"""

from __future__ import annotations

from typing import cast

from fastapi import FastAPI
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.scheduler.locks import (
    LeaderLock,
    PostgresAdvisoryLock,
    SingleProcessLock,
    build_leader_lock,
)
from app.scheduler.service import SchedulerService
from app.settings import Settings

__all__ = [
    "LeaderLock",
    "PostgresAdvisoryLock",
    "SchedulerService",
    "SingleProcessLock",
    "build_leader_lock",
    "install_scheduler",
]


def install_scheduler(application: FastAPI) -> SchedulerService:
    """Attach a scheduler to *application*'s lifespan and probes.

    Idempotent. ``create_app`` calls this, so a second call on the same
    application returns the installed service rather than stacking a
    second background thread on the same lifespan.
    """
    installed = getattr(application.state, "scheduler", None)
    if isinstance(installed, SchedulerService):
        return installed

    service = SchedulerService(
        cast("Settings", application.state.settings),
        cast("Engine", application.state.engine),
        cast("sessionmaker[Session]", application.state.session_factory),
    )
    application.state.scheduler = service
    service.register_probe()
    application.router.on_startup.append(service.start)
    application.router.on_shutdown.append(service.shutdown)
    return service
