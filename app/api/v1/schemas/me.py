from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from app.api.v1.schemas.common import ApiModel
from app.db.models import MAX_MUTED_TERM_LENGTH, Layout, Theme

#: Bound on how many terms of one kind a user may mute. Generous for a
#: reader — nobody curates a hundred words by hand — and low enough that
#: the feed's mute predicate stays a bounded expression rather than
#: something a single account can make arbitrarily large.
MAX_MUTED_TERMS = 100


class UserOut(ApiModel):
    id: int
    github_id: int
    github_login: str
    display_name: str | None
    avatar_url: str | None
    is_admin: bool
    created_at: datetime


#: Shared by the response and the patch, so the two cannot disagree about
#: what a term may be. This bounds *length* and nothing else: validation
#: runs before `app.services.preferences` normalises, so `"   "` satisfies
#: `min_length=1` here and is empty by the time it would be stored. Whether
#: a term survives normalisation is therefore settled there, where the
#: normalising happens, rather than asserted here where it cannot be.
MutedTerms = list[Annotated[str, StringConstraints(min_length=1, max_length=MAX_MUTED_TERM_LENGTH)]]


class PreferencesOut(ApiModel):
    theme: Theme
    layout: Layout
    max_visible_cards: int
    onboarding_completed: bool
    topics: list[str] = Field(description="Selected topic slugs")
    sources: list[str] = Field(description="Enabled source slugs")
    muted_words: MutedTerms = Field(
        description="Words and phrases hidden from the feed, normalised and sorted"
    )
    muted_tags: MutedTerms = Field(description="Topic slugs hidden from the feed, sorted")


class PreferencesPatch(BaseModel):
    """Partial update: absent fields are left untouched. An explicit
    empty list clears the corresponding selection."""

    theme: Theme | None = None
    layout: Layout | None = None
    max_visible_cards: int | None = Field(default=None, ge=1, le=100)
    onboarding_completed: bool | None = None
    topics: list[str] | None = None
    sources: list[str] | None = None
    #: Replace-the-whole-list, like `topics` and `sources` above: an
    #: explicit `[]` unmutes everything and an absent field changes
    #: nothing. Words are free text and are normalised rather than
    #: validated — the point of muting is language the catalogue has never
    #: heard of. Tags are topic slugs and are checked against the
    #: catalogue, because a muted tag that matches no topic is a typo that
    #: would silently mute nothing.
    muted_words: Annotated[MutedTerms, Field(max_length=MAX_MUTED_TERMS)] | None = None
    muted_tags: Annotated[MutedTerms, Field(max_length=MAX_MUTED_TERMS)] | None = None


class MeResponse(BaseModel):
    user: UserOut
    preferences: PreferencesOut
