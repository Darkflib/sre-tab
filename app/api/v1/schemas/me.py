from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from app.api.v1.schemas.common import ApiModel
from app.db.models import MAX_LANGUAGE_CODE_LENGTH, MAX_MUTED_TERM_LENGTH, Layout, Theme
from app.ingest.normalise import MAX_URL_LENGTH

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

#: What a URL mute may be on the way *in*, which is not what it is once
#: stored, and the difference is the reason this is a type of its own. A
#: reader pastes an article URL; `app.services.preferences` reduces it to a
#: host and at most one path segment, and only that has to fit the column.
#: Bounded at `MutedTerms`' sixty-four, an ordinary paste would be a 422
#: before the reduction that makes it fit could run. The response carries
#: the reduced terms, so it stays `MutedTerms`.
MutedUrlInputs = list[Annotated[str, StringConstraints(min_length=1, max_length=MAX_URL_LENGTH)]]


#: Language codes as the detector emits them. Length-bounded here and
#: checked against the detector's label set in `app.services.preferences`,
#: for the reason `muted_tags` is checked against the catalogue.
LanguageCodes = list[
    Annotated[str, StringConstraints(min_length=1, max_length=MAX_LANGUAGE_CODE_LENGTH)]
]

#: Bound on how many languages a reader may list. Far above anyone's real
#: list, and it keeps the feed's `IN (...)` a bounded expression.
MAX_LANGUAGES = 32


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
    muted_urls: MutedTerms = Field(
        description=(
            "Hosts, or a host and its first path segment, whose links are hidden "
            "from the feed, sorted"
        )
    )
    languages: LanguageCodes = Field(
        description=(
            "Languages this reader reads, as detector codes, sorted. Empty means every "
            "language; otherwise an item detected in another language is hidden, and an "
            "item with no confident detection is always shown"
        )
    )


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
    #: would silently mute nothing. URLs are anything from a bare host to a
    #: pasted article link, and each is reduced before it is stored — the
    #: response says what it became.
    muted_words: Annotated[MutedTerms, Field(max_length=MAX_MUTED_TERMS)] | None = None
    muted_tags: Annotated[MutedTerms, Field(max_length=MAX_MUTED_TERMS)] | None = None
    muted_urls: Annotated[MutedUrlInputs, Field(max_length=MAX_MUTED_TERMS)] | None = None
    #: Replace-the-whole-list as well; `[]` reads every language.
    languages: Annotated[LanguageCodes, Field(max_length=MAX_LANGUAGES)] | None = None


class MeResponse(BaseModel):
    user: UserOut
    preferences: PreferencesOut
