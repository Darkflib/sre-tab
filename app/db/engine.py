"""Engine construction for SQLite (dev/tests) and PostgreSQL (production)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, create_engine, event


def create_db_engine(url: str) -> Engine:
    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        # TestClient drives requests from worker threads.
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            # One shared in-memory database rather than one per pooled
            # connection.
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool

    engine = create_engine(url, **kwargs)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _enable_sqlite_fks(dbapi_connection: Any, _record: Any) -> None:
            # SQLite ships with FK enforcement off; the ondelete CASCADEs
            # in the models (DELETE /me relies on them) need it on.
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine
