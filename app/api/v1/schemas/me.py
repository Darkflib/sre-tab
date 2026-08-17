from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.api.v1.schemas.common import ApiModel
from app.db.models import Layout, Theme


class UserOut(ApiModel):
    id: int
    github_id: int
    github_login: str
    display_name: str | None
    avatar_url: str | None
    is_admin: bool
    created_at: datetime


class PreferencesOut(ApiModel):
    theme: Theme
    layout: Layout
    max_visible_cards: int
    onboarding_completed: bool
    topics: list[str] = Field(description="Selected topic slugs")
    sources: list[str] = Field(description="Enabled source slugs")


class PreferencesPatch(BaseModel):
    """Partial update: absent fields are left untouched. An explicit
    empty list clears the corresponding selection."""

    theme: Theme | None = None
    layout: Layout | None = None
    max_visible_cards: int | None = Field(default=None, ge=1, le=100)
    onboarding_completed: bool | None = None
    topics: list[str] | None = None
    sources: list[str] | None = None


class MeResponse(BaseModel):
    user: UserOut
    preferences: PreferencesOut
