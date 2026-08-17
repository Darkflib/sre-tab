"""Sources listing."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.api.v1.schemas import ErrorResponse, SourcesResponse
from app.db.session import get_db
from app.services import sources as sources_service

router = APIRouter(tags=["sources"])


@router.get(
    "/sources",
    response_model=SourcesResponse,
    responses={401: {"model": ErrorResponse, "description": "Not signed in"}},
)
def list_sources(user: CurrentUser, db: Annotated[Session, Depends(get_db)]) -> SourcesResponse:
    """Enabled sources and topic metadata for the settings screen."""
    return sources_service.list_catalogue(db)
