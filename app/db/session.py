"""Session factory and the request-scoped session dependency.

Sessions are sync by contract and reach request code only through
``get_db`` — routes never open sessions themselves (AGENTS.md,
data-access rules).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

from fastapi import Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_db(request: Request) -> Iterator[Session]:
    """FastAPI dependency yielding one Session per request."""
    factory = cast("sessionmaker[Session]", request.app.state.session_factory)
    with factory() as session:
        yield session
