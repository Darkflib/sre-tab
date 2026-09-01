"""FastAPI application factory."""

from __future__ import annotations

import threading
from collections.abc import Callable
from importlib.metadata import version as package_version

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

# How long the readiness probe waits for the database before calling it
# unready. Comfortably longer than any healthy `SELECT 1` and comfortably
# shorter than the reverse proxy's response-header timeout (30s in
# deploy/Caddyfile), so a stalled dependency shows up as a 503 we wrote
# rather than a 504 the proxy invented.
DATABASE_PROBE_TIMEOUT_SECONDS = 5.0


class BoundedCheck:
    """Run a probe on a single background thread, giving up after a deadline.

    A *stopped* database fails a readiness query immediately — the connection
    is refused and there is an exception to report. A *frozen* one (a paused
    container, a black-holed route, a stalled volume) does not: the TCP
    connection is still established and still being acknowledged by the peer's
    kernel, so nothing at the socket or libpq layer ever times out. The probe
    blocks in ``recv`` and ``/healthz`` stops answering, which makes a sick
    dependency look exactly like a sick application. Neither ``connect_timeout``
    nor TCP keepalives help here, because the connection is not broken; it is
    merely unanswered.

    Waiting on a worker with a deadline is what bounds it. Exactly one worker,
    on purpose: a stalled check keeps that thread, so callers coalesce onto the
    in-flight attempt instead of opening a fresh connection each time, and
    polling a frozen database cannot turn into a thread or connection leak. The
    thread is a daemon so a check that will never return cannot hold up
    interpreter shutdown either — which matters, because this process is PID 1
    of a container.
    """

    def __init__(self, check: Callable[[], ProbeResult], timeout: float) -> None:
        self._check = check
        self._timeout = timeout
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._running = False
        self._result = ProbeResult(ok=False, detail="not yet checked")

    def __call__(self) -> ProbeResult:
        with self._lock:
            if not self._running:
                self._running = True
                self._done.clear()
                threading.Thread(target=self._run, name="healthz-probe", daemon=True).start()
            done = self._done

        if not done.wait(self._timeout):
            return ProbeResult(ok=False, detail=f"timeout after {self._timeout:g}s")
        with self._lock:
            return self._result

    def _run(self) -> None:
        try:
            result = self._check()
        except Exception as exc:  # pragma: no cover - _check catches its own
            result = ProbeResult(ok=False, detail=f"{type(exc).__name__}")
        # _running is cleared before the event is set, so a caller arriving
        # immediately after starts a fresh attempt rather than reading this one.
        with self._lock:
            self._result = result
            self._running = False
            self._done.set()


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
        # Read from the installed distribution rather than repeated here.
        # These were two sources of truth and they drifted: the 1.0.0 release
        # bumped pyproject.toml and package.json and left this at 0.1.0, so
        # the published contract went on identifying the application as the
        # version before the one that shipped. Now a bump has one place to
        # happen, and `tests/test_openapi.py` turns forgetting to regenerate
        # the committed document into a failing test rather than a silent
        # disagreement.
        version=package_version("sre-tab"),
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
    probes.register_readiness(
        "database",
        BoundedCheck(lambda: _database_probe(session_factory), DATABASE_PROBE_TIMEOUT_SECONDS),
    )

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
