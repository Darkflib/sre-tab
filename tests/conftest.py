"""Root fixtures — Phase 0 property, frozen.

Phase 1 agents never edit this file; extra fixtures live in
tests/<area>/conftest.py (AGENTS.md).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.engine import create_db_engine
from app.db.models import Base, User
from app.db.session import build_session_factory
from app.main import create_app
from app.settings import Settings


@pytest.fixture
def settings() -> Settings:
    """Deterministic test settings; never reads .env."""
    return Settings(  # type: ignore[call-arg]  # _env_file is a pydantic-settings init kwarg
        _env_file=None,
        database_url="sqlite://",
        session_secret=SecretStr("test-session-secret"),
        github_client_id="test-client-id",
        github_client_secret=SecretStr("test-client-secret"),
        allowed_github_ids=[1000001],
    )


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Fresh in-memory SQLite database per test, schema from the models."""
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    with build_session_factory(engine)() as session:
        yield session


@pytest.fixture
def app(settings: Settings, engine: Engine) -> FastAPI:
    return create_app(settings, engine=engine)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def test_user(db_session: Session) -> User:
    user = User(github_id=1000001, github_login="octocat", display_name="Octo Cat")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def authed_client(app: FastAPI, client: TestClient, test_user: User) -> Iterator[TestClient]:
    """Client signed in as ``test_user``.

    The dependency override is a stand-in until agent A lands real auth;
    the fixture's shape (a client plus the ``test_user`` row) is final.
    """
    app.dependency_overrides[get_current_user] = lambda: test_user
    yield client
    app.dependency_overrides.pop(get_current_user, None)
