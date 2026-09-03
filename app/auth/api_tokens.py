"""Long-lived per-user API tokens: minting, resolution, and revocation.

The counterpart of :mod:`app.auth.sessions`, for the credential a user
hands to another application rather than the one a browser carries. The
same two disciplines apply, and for the same reasons.

*Only the digest is stored.* ``app.security.tokens`` mints the value and
hashes it; the raw token exists in the creation response and nowhere
else, so there is nothing in the database to steal and nothing on the
server to show a user who lost it.

*Liveness is decided in SQL, in one statement.* Revocation and expiry are
predicates in the ``WHERE`` clause rather than Python comparisons after
the load. SQLite hands back naive datetimes for ``DateTime(timezone=True)``
columns, so a Python-side comparison against an aware ``now`` would raise
on SQLite and not on PostgreSQL — a portability trap that would show up
as a 500 in development and never in the tests that matter. Binding the
moment as a parameter and letting the database compare keeps the answer
identical on both engines.

Everything here participates in the caller's transaction and never
commits (AGENTS.md, "Transactions"). The route commits; so does
:mod:`app.auth.bearer`, which opens its own session for the
``last_used_at`` write because middleware has no route to borrow one
from.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, or_, select, update
from sqlalchemy.orm import Session

from app.db.models import ApiToken, ApiTokenScope, User
from app.security.tokens import (
    API_TOKEN_PREFIX,
    api_token_display_prefix,
    generate_api_token,
    hash_api_token,
)


@dataclass(frozen=True)
class IssuedToken:
    """A freshly minted token and its row.

    ``value`` is the only place the raw token ever exists after
    :func:`create_token` returns. It is not stored, not logged, and not
    recoverable; the caller serialises it into the creation response and
    lets it go.
    """

    token: ApiToken
    value: str


def create_token(
    db: Session,
    user: User,
    *,
    label: str,
    scope: ApiTokenScope,
    expires_at: datetime | None = None,
) -> IssuedToken:
    """Mint a token for ``user``. The caller commits."""
    value = generate_api_token()
    token = ApiToken(
        user_id=user.id,
        label=label,
        token_hash=hash_api_token(value),
        display_prefix=api_token_display_prefix(value),
        scope=scope,
        expires_at=expires_at,
    )
    db.add(token)
    db.flush()
    return IssuedToken(token=token, value=value)


def resolve_token(db: Session, presented: str, *, now: datetime | None = None) -> ApiToken | None:
    """The live token row behind ``presented``, or None.

    "Live" means the row exists, has not been revoked, and either has no
    expiry or has not reached it — all asserted in the query, so a
    revoked or expired token can never authenticate.

    The prefix check ahead of the query is not an optimisation dressed up
    as a guard, and it must not become one: a value without the prefix
    cannot be a token we minted, so answering None without a lookup is
    the same answer by a shorter path. Every rejection here returns None,
    which is what lets the caller refuse unknown, malformed, revoked, and
    expired tokens identically.
    """
    if not presented.startswith(API_TOKEN_PREFIX):
        return None
    moment = now or datetime.now(UTC)
    statement = select(ApiToken).where(
        ApiToken.token_hash == hash_api_token(presented),
        ApiToken.revoked_at.is_(None),
        or_(ApiToken.expires_at.is_(None), ApiToken.expires_at > moment),
    )
    return db.execute(statement).scalar_one_or_none()


def list_tokens(db: Session, user: User) -> Sequence[ApiToken]:
    """``user``'s live tokens, newest first.

    Revoked rows are excluded rather than shown greyed out. A revoked
    token is inert — :func:`resolve_token` refuses it — so listing it
    would offer the owner a decision they no longer have. Expired ones
    *are* listed, because "this stopped working last March" is
    information the owner wants and can act on by deleting it.
    """
    statement = (
        select(ApiToken)
        .where(ApiToken.user_id == user.id, ApiToken.revoked_at.is_(None))
        .order_by(ApiToken.created_at.desc(), ApiToken.id.desc())
    )
    return db.scalars(statement).all()


def revoke_token(db: Session, user: User, token_id: int) -> bool:
    """Revoke one of ``user``'s tokens. True if one was live.

    ``user_id`` is a predicate in the ``UPDATE`` rather than a check
    after loading the row. Another user's token id and an id that never
    existed then take the same path and produce the same result, so this
    cannot be used to ask whether a given token id exists — the same
    reasoning ``tests/api/test_isolation.py`` pins for bookmarks.

    The caller commits.
    """
    result = cast(
        "CursorResult[Any]",
        db.execute(
            update(ApiToken)
            .where(
                ApiToken.id == token_id,
                ApiToken.user_id == user.id,
                ApiToken.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        ),
    )
    return bool(result.rowcount)


def touch_last_used(db: Session, token_id: int, *, now: datetime | None = None) -> None:
    """Record that ``token_id`` was just presented successfully.

    A bulk ``UPDATE`` rather than a load-and-assign: nothing needs the
    ORM object back, and this runs on every authenticated API request.
    The caller commits.
    """
    db.execute(
        update(ApiToken)
        .where(ApiToken.id == token_id)
        .values(last_used_at=now or datetime.now(UTC))
    )
