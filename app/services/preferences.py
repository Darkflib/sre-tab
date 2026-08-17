"""Preference profile service — the seam between agents A and C.

Agent A owns ``app/api/v1/me.py`` and calls these functions; agent C owns
this module and implements them. The signatures below are frozen: neither
agent changes them without the coordinator, because both sides are written
in parallel against this contract.

Every function participates in the caller's transaction and must not
commit; the request-scoped session dependency owns commit and rollback.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import Select, delete, select
from sqlalchemy.orm import Session

from app.api.v1.schemas import PreferencesOut, PreferencesPatch
from app.db.models import (
    Source,
    Topic,
    User,
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
    ``ValueError`` — agent A maps that to HTTP 422.
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

    # Flush, never commit: the read-back below has to see the update, but
    # the transaction boundary belongs to the caller (agent A's route).
    db.flush()
    return _to_out(db, user, profile)


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
