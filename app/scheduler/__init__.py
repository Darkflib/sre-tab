"""In-process scheduling for feed refresh and retention pruning.

Wiring is one line, and it belongs to Phase 2 rather than here:
``app/main.py`` is Phase 0 property, so this package cannot install
itself. Integration adds

.. code-block:: python

    from app.scheduler import install_scheduler

    install_scheduler(application)

to :func:`app.main.create_app`, after ``application.state`` is
populated. Until that line exists the scheduler never starts and no
``scheduler`` readiness probe appears in ``/api/v1/healthz``.
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
    """Attach a scheduler to *application*'s lifespan and probes."""
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
