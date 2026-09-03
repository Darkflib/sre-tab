"""Preference profile service — the seam between agents A and C.

Agent A owns ``app/api/v1/me.py`` and calls these functions; agent C owns
this module and implements them. The signatures below are frozen: neither
agent changes them without the coordinator, because both sides are written
in parallel against this contract.

Every function participates in the caller's transaction and must not
commit — the repository convention is that whoever *opened* the session
owns it (AGENTS.md, "Transactions"). For these functions that is the
route, via ``app.db.session.get_db``: ``get_db`` does not commit, it
closes the session, and closing rolls back anything the route did not
commit.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import Select, delete, select
from sqlalchemy.orm import Session

from app.api.v1.schemas import PreferencesOut, PreferencesPatch
from app.db.models import (
    MAX_MUTED_TERM_LENGTH,
    MuteKind,
    Source,
    Topic,
    User,
    UserMutedTerm,
    UserPreferences,
    UserPreferenceSource,
    UserPreferenceTopic,
)
from app.services.errors import UnknownSlugError
from app.services.upsert import insert_ignore

# Default enabled sources at first sign-in.
#
# Deliberately not "every source". Publication rates across the v1
# catalogue differ by more than an order of magnitude: BBC News, the
# Guardian, and Ars Technica push tens of items an hour, while Lobsters
# and LWN push a handful a day. A feed ordered by publication time with
# everything switched on is a general-news feed within the hour, and the
# low-volume, high-signal sources the product exists to surface are the
# ones buried — the user never sees the item they would have valued, so
# they never learn that filtering is what they needed.
#
# The defensible starting set is therefore the developer core the PRD
# itself names ("Hacker News, Lobsters, Dev.to, and a limited set of
# administrator-approved engineering blogs"): comparable publication
# rates, so no member drowns another, and it matches what someone signing
# in to a *developer* news dashboard expects to see first. General news
# and per-tag Medium feeds are one tick away in onboarding — opt-in, so
# the user who widens the feed is the one who chose the volume.
#
# Slugs, not IDs, and intersected with what the instance actually holds:
# the catalogue is seeded by the Phase 2 operator CLI, which must keep
# these slugs in step. An instance that seeds none of them lands on an
# empty selection, which the feed reads as "no narrowing" rather than as
# an empty feed.
DEFAULT_SOURCE_SLUGS: tuple[str, ...] = ("hacker-news", "lobsters", "dev-to", "lwn")


def ensure_profile(db: Session, user: User) -> None:
    """Create the instance-default profile if the user has none.

    Idempotent: safe to call on every sign-in. Agent A calls this after
    creating or updating the user record at OAuth callback.

    Defaults must not enable every source at once — see the volume
    asymmetry note in PLAN-v1.md.
    """
    _get_or_create_profile(db, user)


def load_profile(db: Session, user: User) -> PreferencesOut:
    """Return the user's profile, with topic and source **slugs**.

    Assumes :func:`ensure_profile` has run for this user.
    """
    profile = _get_or_create_profile(db, user)
    return _to_out(db, user, profile)


def apply_patch(db: Session, user: User, patch: PreferencesPatch) -> PreferencesOut:
    """Apply a partial update and return the resulting profile.

    Absent fields are untouched; an explicit empty list clears that
    selection. Unknown or disabled topic/source slugs are rejected with
    ``ValueError`` — agent A maps that to HTTP 422 — and so is a muted tag
    naming no topic, for the reason recorded at that branch.
    """
    profile = _get_or_create_profile(db, user)

    # Scalars: ``None`` means absent, and the schema has no nullable
    # scalar, so "absent" and "left alone" coincide.
    if patch.theme is not None:
        profile.theme = patch.theme
    if patch.layout is not None:
        profile.layout = patch.layout
    if patch.max_visible_cards is not None:
        profile.max_visible_cards = patch.max_visible_cards
    if patch.onboarding_completed is not None:
        profile.onboarding_completed = patch.onboarding_completed

    # Collections: an explicit ``[]`` clears the selection and must not be
    # confused with the field being absent, so the ``is not None`` test is
    # load-bearing rather than defensive.
    if patch.topics is not None:
        known = _slug_ids(db, select(Topic.slug, Topic.id).where(Topic.enabled.is_(True)))
        topic_ids = _resolve(known, patch.topics, "topic")
        db.execute(delete(UserPreferenceTopic).where(UserPreferenceTopic.user_id == user.id))
        insert_ignore(
            db,
            UserPreferenceTopic,
            [{"user_id": user.id, "topic_id": topic_id} for topic_id in topic_ids],
        )

    if patch.sources is not None:
        known = _slug_ids(db, select(Source.slug, Source.id).where(Source.enabled.is_(True)))
        source_ids = _resolve(known, patch.sources, "source")
        db.execute(delete(UserPreferenceSource).where(UserPreferenceSource.user_id == user.id))
        insert_ignore(
            db,
            UserPreferenceSource,
            [{"user_id": user.id, "source_id": source_id} for source_id in source_ids],
        )

    if patch.muted_words is not None:
        _replace_mutes(db, user, MuteKind.WORD, _mute_terms(patch.muted_words))

    if patch.muted_tags is not None:
        terms = _mute_terms(patch.muted_tags)
        # Validated against the catalogue, unlike words. A muted word the
        # catalogue has never heard of is the point; a muted *tag* that
        # matches no topic is a typo that would silently mute nothing, and
        # a preference which reports success and does nothing is worse than
        # a 422. `_resolve` is reused for the error it raises, not for the
        # ids — mutes store slugs, so a topic renamed out from under one
        # stops applying rather than silently muting something else.
        #
        # Every topic, not only the enabled ones, and that is the fix for a
        # trap rather than a loosening. The feed's mute predicate matches
        # slugs and never consults `topics.enabled`, so a topic an operator
        # retires goes on hiding items. Validating against enabled topics
        # then made every patch carrying that slug a 422 — and since the
        # field is replace-the-whole-list, that is every patch changing any
        # *other* mute. The mute kept working and could not be removed.
        # Accepting the retired slug is what makes the state the API
        # already stores a state the API will still take back.
        known = _slug_ids(db, select(Topic.slug, Topic.id))
        _resolve(known, terms, "topic")
        _replace_mutes(db, user, MuteKind.TAG, terms)

    # Flush, never commit: the read-back below has to see the update, but
    # the transaction boundary belongs to the caller (agent A's route).
    db.flush()
    return _to_out(db, user, profile)


def normalise_terms(terms: Iterable[str]) -> list[str]:
    """Case-folded, whitespace-collapsed, deduplicated, sorted.

    Normalising rather than storing what was typed is what makes the
    table's ``(user_id, kind, term)`` primary key mean what a reader would
    expect: muting "Football" and then "football" is one mute, not two,
    and neither is a second row that does the same job.

    Purely a normaliser: it can return the empty string, and deciding what
    that means belongs to :func:`_mute_terms`.
    """
    return sorted({" ".join(term.split()).casefold() for term in terms})


def _mute_terms(terms: Iterable[str]) -> list[str]:
    """Normalised, and re-checked against the column the result must fit.

    The re-check is not belt and braces. ``casefold`` is not
    length-preserving — ``"ß"`` becomes ``"ss"`` — so sixty-four characters
    of it satisfy the schema's ``max_length`` on the way in and are a
    hundred and twenty-eight by the time anything stores them. Past
    ``VARCHAR(64)``: PostgreSQL raises ``DataError``, which is not a
    ``ValueError`` and is therefore a 500, and SQLite (which does not
    enforce the width) takes the row and fails building the *response*
    instead. That failure happened to surface as a 422, because
    ``pydantic.ValidationError`` subclasses ``ValueError`` and the route
    maps that — a right answer for a wrong reason, with a body naming
    ``PreferencesOut`` at a client that never mentioned it.

    Refused rather than truncated: a shortened mute matches more than the
    reader asked for, and does so silently, which is the failure mode this
    whole feature is written against.

    A term that normalises to *nothing* is refused for the same reason and
    a sharper one. It was dropped at first, which is safe in the sense
    that matters — an empty term is a substring of every item and must
    never be stored — and unsafe in a way the test for it could not see:
    dropping turns ``["  "]`` into ``[]``, which is the wire form of
    "unmute everything". A request that looks like adding one mute removed
    every mute the reader had. Refusing keeps the list untouched, which is
    what a reader who typed a space by accident would expect of it.
    """
    normalised = normalise_terms(terms)
    if "" in normalised:
        raise ValueError("a muted term cannot be empty; send an empty list to unmute everything")
    over = [term for term in normalised if len(term) > MAX_MUTED_TERM_LENGTH]
    if over:
        raise ValueError(
            f"muted term is longer than {MAX_MUTED_TERM_LENGTH} characters once "
            f"case-folded: {over[0][:32]!r}…"
        )
    return normalised


def _replace_mutes(db: Session, user: User, kind: MuteKind, terms: Sequence[str]) -> None:
    """Replace this user's mutes of one kind. Delete-then-insert, like the
    topic and source selections above: a patch carries the whole list, so
    reconciling additions and removals separately would be more code to
    reach the same rows."""
    db.execute(
        delete(UserMutedTerm).where(UserMutedTerm.user_id == user.id, UserMutedTerm.kind == kind)
    )
    insert_ignore(
        db,
        UserMutedTerm,
        [{"user_id": user.id, "kind": kind, "term": term} for term in terms],
    )


def muted_terms(user_id: int, kind: MuteKind) -> Select[tuple[str]]:
    """This user's muted terms of one kind, ordered for stability."""
    return (
        select(UserMutedTerm.term)
        .where(UserMutedTerm.user_id == user_id, UserMutedTerm.kind == kind)
        .order_by(UserMutedTerm.term)
    )


def selected_topic_slugs(user_id: int) -> Select[tuple[str]]:
    """Slugs of the topics this user selected, ordered for stability."""
    return (
        select(Topic.slug)
        .join(UserPreferenceTopic, UserPreferenceTopic.topic_id == Topic.id)
        .where(UserPreferenceTopic.user_id == user_id)
        .order_by(Topic.slug)
    )


def selected_source_slugs(user_id: int) -> Select[tuple[str]]:
    """Slugs of the sources this user enabled, ordered for stability."""
    return (
        select(Source.slug)
        .join(UserPreferenceSource, UserPreferenceSource.source_id == Source.id)
        .where(UserPreferenceSource.user_id == user_id)
        .order_by(Source.slug)
    )


def _get_or_create_profile(db: Session, user: User) -> UserPreferences:
    profile = db.scalar(select(UserPreferences).where(UserPreferences.user_id == user.id))
    if profile is not None:
        # Seeding defaults a second time would resurrect selections the
        # user deliberately cleared, so the profile row gates all of it.
        return profile

    # Column defaults supply theme, layout, card count, and onboarding
    # state; ON CONFLICT absorbs two concurrent first sign-ins.
    insert_ignore(db, UserPreferences, [{"user_id": user.id}])

    # Every enabled topic starts selected. The volume asymmetry is a
    # source-rate problem answered at the source dimension; narrowing
    # topics as well would mean a user who later enables BBC News sees
    # nothing from it until they change a second, unrelated setting —
    # which reads as a bug, not as a default.
    topic_ids = db.scalars(select(Topic.id).where(Topic.enabled.is_(True))).all()
    insert_ignore(
        db,
        UserPreferenceTopic,
        [{"user_id": user.id, "topic_id": topic_id} for topic_id in topic_ids],
    )

    source_ids = db.scalars(
        select(Source.id).where(Source.enabled.is_(True), Source.slug.in_(DEFAULT_SOURCE_SLUGS))
    ).all()
    insert_ignore(
        db,
        UserPreferenceSource,
        [{"user_id": user.id, "source_id": source_id} for source_id in source_ids],
    )

    db.flush()
    created = db.scalar(select(UserPreferences).where(UserPreferences.user_id == user.id))
    if created is None:  # pragma: no cover - the insert above just wrote it
        raise RuntimeError("preference profile vanished between insert and read")
    return created


def _to_out(db: Session, user: User, profile: UserPreferences) -> PreferencesOut:
    return PreferencesOut(
        theme=profile.theme,
        layout=profile.layout,
        max_visible_cards=profile.max_visible_cards,
        onboarding_completed=profile.onboarding_completed,
        topics=list(db.scalars(selected_topic_slugs(user.id)).all()),
        sources=list(db.scalars(selected_source_slugs(user.id)).all()),
        muted_words=list(db.scalars(muted_terms(user.id, MuteKind.WORD)).all()),
        muted_tags=list(db.scalars(muted_terms(user.id, MuteKind.TAG)).all()),
    )


def _slug_ids(db: Session, statement: Select[tuple[str, int]]) -> dict[str, int]:
    return dict(db.execute(statement).tuples().all())


def _resolve(known: dict[str, int], requested: Iterable[str], kind: str) -> list[int]:
    """Map slugs to ids, rejecting the whole patch if any is unknown.

    Rejecting rather than silently dropping: a typo that quietly vanishes
    leaves the user staring at a setting they believe they saved.
    """
    unknown = sorted({slug for slug in requested if slug not in known})
    if unknown:
        raise UnknownSlugError(f"unknown or disabled {kind} slugs: {', '.join(unknown)}")
    # dict.fromkeys de-duplicates a repeated slug without losing order.
    return [known[slug] for slug in dict.fromkeys(requested)]
