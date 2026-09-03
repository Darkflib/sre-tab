"""API-token management, mounted under ``/me``.

Tokens are per-user, so they are a user's own business rather than an
operator's: these three routes are what the Settings screen drives, and
there is no CLI half in this change.

The router is included by :mod:`app.api.v1.me` rather than by the ``/api/v1``
aggregator. ``app/api/v1/router.py`` is Phase 0 property and frozen
(AGENTS.md), and mounting here needs no exception to that: ``/me/tokens``
is where a per-user sub-resource belongs anyway, next to
``/me/preferences``.

Every route carries ``Depends(require_interactive_session)`` at the
*router* level, which refuses a request authenticated by API token —
see that function for why revoking a token should be something a token
cannot undo.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, require_interactive_session
from app.api.v1.schemas import (
    ApiTokenCreate,
    ApiTokenCreated,
    ApiTokenList,
    ApiTokenOut,
    ErrorResponse,
)
from app.auth import api_tokens
from app.db.session import get_db

router = APIRouter(
    prefix="/tokens",
    tags=["api-tokens"],
    dependencies=[Depends(require_interactive_session)],
)

log = structlog.get_logger(__name__)

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Not signed in"},
    403: {"model": ErrorResponse, "description": "Not a signed-in session"},
}


@router.get("", response_model=ApiTokenList, responses=_ERRORS)
def list_api_tokens(user: CurrentUser, db: Annotated[Session, Depends(get_db)]) -> ApiTokenList:
    """The caller's own tokens. Never anybody else's, and never a value."""
    return ApiTokenList(
        tokens=[ApiTokenOut.model_validate(token) for token in api_tokens.list_tokens(db, user)]
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiTokenCreated,
    responses=_ERRORS,
)
def create_api_token(
    user: CurrentUser,
    body: ApiTokenCreate,
    db: Annotated[Session, Depends(get_db)],
) -> ApiTokenCreated:
    """Mint a token and return it. This response is the only sight of it.

    The expiry is computed here rather than accepted as a timestamp: a
    client sending an absolute moment would be asserting a shared clock,
    and the only clock that matters is the one the ``expires_at``
    predicate is evaluated against.
    """
    expires_at = (
        datetime.now(UTC) + timedelta(days=body.expires_in_days)
        if body.expires_in_days is not None
        else None
    )
    issued = api_tokens.create_token(
        db, user, label=body.label, scope=body.scope, expires_at=expires_at
    )
    db.commit()
    # Label and scope, never the value or the digest.
    log.info(
        "api_token_created",
        user_id=user.id,
        token_id=issued.token.id,
        scope=str(issued.token.scope),
        expires=expires_at is not None,
    )
    return ApiTokenCreated(token=ApiTokenOut.model_validate(issued.token), value=issued.value)


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
def revoke_api_token(
    user: CurrentUser, token_id: int, db: Annotated[Session, Depends(get_db)]
) -> Response:
    """Revoke a token. Revoking one that is not yours is a 204 no-op.

    Not a 404, on the reasoning ``tests/api/test_isolation.py`` already
    pins for bookmarks: answering differently for "not yours" and "never
    existed" would confirm a guessed id. The revocation either happened
    or there was nothing of yours to revoke, and from outside those look
    the same.
    """
    if api_tokens.revoke_token(db, user, token_id):
        db.commit()
        log.info("api_token_revoked", user_id=user.id, token_id=token_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
