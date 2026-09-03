from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from app.api.v1.schemas.common import ApiModel
from app.db.models import ApiTokenScope

TOKEN_LABEL_MAX_LENGTH = 100

#: Upper bound on a requested lifetime, in days. Ten years is not a
#: security control — a token with no expiry at all is the ordinary case
#: and is allowed — it is a bound on a number a client can send, so that
#: "expires in 10**9 days" is a 422 rather than a datetime overflow.
TOKEN_MAX_EXPIRY_DAYS = 3650

TokenLabel = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TOKEN_LABEL_MAX_LENGTH)
]


class ApiTokenOut(ApiModel):
    """A token as its owner sees it. Never carries the token."""

    id: int
    label: str
    scope: ApiTokenScope
    display_prefix: str = Field(
        description="The token's non-secret leading characters, for telling two tokens apart"
    )
    created_at: datetime
    last_used_at: datetime | None = Field(
        description="When this token was last presented successfully; null if never"
    )
    expires_at: datetime | None = Field(description="When it stops working; null if it does not")


class ApiTokenList(ApiModel):
    tokens: list[ApiTokenOut]


class ApiTokenCreate(BaseModel):
    label: TokenLabel = Field(description="What this token is for, in the owner's own words")
    # No default. The two scopes differ by the whole of their blast
    # radius, so the choice is made rather than inherited: a client that
    # says nothing gets a 422, not the convenient one.
    scope: ApiTokenScope = Field(description="read: safe methods only. full: everything.")
    expires_in_days: int | None = Field(
        default=None,
        ge=1,
        le=TOKEN_MAX_EXPIRY_DAYS,
        description="Optional lifetime in days; omit for a token that does not expire",
    )


class ApiTokenCreated(BaseModel):
    """The one and only response carrying a raw token."""

    token: ApiTokenOut
    value: str = Field(
        description=(
            "The token itself. Shown once, at creation. Only a hash is stored, "
            "so it cannot be retrieved again."
        )
    )
