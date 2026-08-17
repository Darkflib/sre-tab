"""Opt-in suite that needs a real PostgreSQL server.

Everything else in ``tests/`` runs on in-memory SQLite, which is fast and
covers behaviour — but three things in this codebase are PostgreSQL
dialect paths that SQLite cannot exercise at all:

- ``pg_try_advisory_lock`` (the scheduler's leader strategy),
- the PostgreSQL ``ON CONFLICT`` branches in the ingest and status writes,
- the migration, against the engine it will actually run on.

Set ``SRE_TAB_POSTGRES_URL`` to run them; without it the module skips::

    docker run --rm -d --name pg -e POSTGRES_PASSWORD=x -p 55432:5432 postgres:18
    SRE_TAB_POSTGRES_URL=postgresql+psycopg://postgres:x@127.0.0.1:55432/postgres \\
      uv run pytest tests/postgres

The database is used destructively: the fixtures drop and recreate the
schema. Point it at a throwaway server, never at anything real.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.db.engine import create_db_engine
from app.db.models import Base
from app.db.session import build_session_factory

POSTGRES_URL_ENV = "SRE_TAB_POSTGRES_URL"

pytestmark = pytest.mark.skipif(
    not os.environ.get(POSTGRES_URL_ENV),
    reason=f"{POSTGRES_URL_ENV} is not set; PostgreSQL integration tests are opt-in",
)


def postgres_url() -> str:
    url = os.environ.get(POSTGRES_URL_ENV)
    if not url:  # pragma: no cover - the skipif above covers this
        pytest.skip(f"{POSTGRES_URL_ENV} is not set")
    return url


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[Engine]:
    engine = create_db_engine(postgres_url())
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def pg_clean(pg_engine: Engine) -> Engine:
    """Empty every table before each test, cheaply and in one statement."""
    tables = ", ".join(table.name for table in reversed(Base.metadata.sorted_tables))
    with pg_engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return pg_engine


@pytest.fixture
def pg_session_factory(pg_clean: Engine) -> sessionmaker[Session]:
    return build_session_factory(pg_clean)


@pytest.fixture
def pg_session(pg_session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with pg_session_factory() as session:
        yield session
