"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker as SessionMaker

from app.api.v1.router import router as v1_router
from app.auth.csrf_middleware import CSRFMiddleware
from app.db.engine import create_db_engine
from app.db.session import build_session_factory
from app.health import ProbeResult, probes
from app.logging import RequestIDMiddleware, configure_logging
from app.middleware import SecurityHeadersMiddleware
from app.settings import Settings, get_settings


def _database_probe(factory: SessionMaker[Session]) -> ProbeResult:
    try:
        with factory() as session:
            session.execute(text("SELECT 1"))
    except Exception as exc:
        return ProbeResult(ok=False, detail=f"{type(exc).__name__}")
    return ProbeResult(ok=True)


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    """Build the application. Tests pass explicit settings and an engine;
    production uses the environment via :func:`get_settings`."""
    settings = settings or get_settings()
    configure_logging(settings)

    application = FastAPI(
        title="Developer News Dashboard API",
        version="0.1.0",
        openapi_url="/api/v1/openapi.json",
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
    )

    engine = engine or create_db_engine(settings.database_url)
    session_factory = build_session_factory(engine)
    application.state.settings = settings
    application.state.engine = engine
    application.state.session_factory = session_factory

    probes.register_liveness("app", lambda: True)
    probes.register_readiness("database", lambda: _database_probe(session_factory))

    # Imported here rather than at module scope: app.scheduler imports the
    # ingest stack, which imports app.settings and the models, and a
    # top-level import would make app.main the entry point for all of it.
    from app.scheduler import install_scheduler

    install_scheduler(application)

    # Added innermost-first (Starlette wraps in reverse). CSRF sits closest
    # to the router so a rejection still travels back out through the
    # request-ID and security-header layers; headers therefore apply to
    # every response, including errors raised inside them.
    application.add_middleware(CSRFMiddleware)
    application.add_middleware(RequestIDMiddleware)
    application.add_middleware(SecurityHeadersMiddleware)

    application.include_router(v1_router, prefix="/api/v1")
    return application


app = create_app()
