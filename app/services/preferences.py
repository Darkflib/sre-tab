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

import re
from collections.abc import Iterable, Sequence

import httpx
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
    UserPreferenceLanguage,
    UserPreferences,
    UserPreferenceSource,
    UserPreferenceTopic,
)
from app.ingest.language import LANGUAGE_CODES
from app.ingest.normalise import InvalidItemURLError, normalise_url
from app.ingest.topicrules import link_host
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

    if patch.muted_urls is not None:
        # Every entry is reduced again, including the ones the client is
        # only sending back because the field is replace-the-whole-list.
        # That is safe because the reduction is idempotent — a stored term
        # reduces to itself — which `url_mute_term` is written to keep true.
        _replace_mutes(
            db, user, MuteKind.URL, sorted({url_mute_term(raw) for raw in patch.muted_urls})
        )

    if patch.languages is not None:
        languages = sorted({code.strip().lower() for code in patch.languages})
        # Refused rather than stored, and this is the sharpest of the
        # validations here: a code the detector never emits matches no
        # item, so a list holding only that code would hide every item
        # with a detected language — "English" typed where "en" was meant
        # would empty the feed.
        unknown = [code for code in languages if code not in LANGUAGE_CODES]
        if unknown:
            raise ValueError(f"unknown language codes: {', '.join(unknown)}")
        db.execute(delete(UserPreferenceLanguage).where(UserPreferenceLanguage.user_id == user.id))
        insert_ignore(
            db,
            UserPreferenceLanguage,
            [{"user_id": user.id, "language": code} for code in languages],
        )

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


#: A scheme, as RFC 3986 spells one. Absent, the input is a bare
#: ``host/segment`` and is read as ``https``; which scheme is irrelevant to
#: the term, since the term carries none and the feed matches both.
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def url_mute_term(raw: str) -> str:
    """The term a URL mute stores for *raw*: a host, and at most one path
    segment of it.

    A reader pastes an article, and what they mean by it is the author or
    the site — ``https://dev.to/jrandom/some-post-1abc?utm_source=x``
    means ``dev.to/jrandom``. Host plus first segment is also what fits the
    column: an article URL does not, and truncating one would mute
    something the reader never named. So the whole input is reduced, and a
    reduction that *still* does not fit is refused with the length in the
    message rather than shortened.

    The input goes through :func:`app.ingest.normalise.normalise_url`
    first, and that is the point rather than a convenience. The term is
    compared against ``feed_items.canonical_url``, which that function
    wrote, so reading the pasted URL by any other rules would produce a
    term in a different dialect from the column it is matched against. It
    also refuses what the column can never contain — an IP literal, a
    ``user:pass@``, a scheme other than ``http(s)`` — so none of those can
    become a mute that matches nothing. The host is then reduced by
    :func:`app.ingest.topicrules.link_host`, the same "is this the same
    host" answer the topic rules give.

    Every save reduces every stored term again, because the field is
    replace-the-whole-list and the client sends back what it was given.
    So the reduction has to be idempotent — a stored term must reduce to
    itself — and two of the refusals below exist for that alone. Each
    refusal is so that a stored term can never mean more than it says:

    - an empty input, which would otherwise read as ``https://`` and fail
      with a message about hosts;
    - a first segment that is empty but not the end of the path
      (``example.com//foo``), which would otherwise reduce to the bare
      host and mute the whole site;
    - a port. A term carries no scheme, so it is read back as ``https``,
      and ``http://example.com:443/`` stored as ``example.com:443`` would
      lose its port to ``normalise_url`` on the next save — ``:443`` is the
      default for the scheme it is re-read under, not the one it came
      from. Dropping the port instead would store a term that does not
      match the link it was pasted from. An article on a non-default port
      is rare enough that saying no is the honest answer;
    - a host that still starts with ``www.`` once one ``www.`` is gone.
      ``link_host`` strips exactly one, so ``www.www.example.com`` becomes
      ``www.example.com`` — and the next save would reduce *that* to
      ``example.com``, quietly widening the mute.

    Lower-cased whole, path included, and deliberately; the feed compares
    against ``lower(canonical_url)`` to match. It is wider than RFC 3986,
    which makes paths case-sensitive, but the segment being muted is an
    author or a section, and ``/JRandom`` and ``/jrandom`` being different
    people is not a case worth a mute that quietly misses the one a
    publisher happened to capitalise. It also keeps the primary key's
    promise the word mutes keep: two pastes a reader would call the same
    are one row. ``normalise_url`` guarantees ASCII (percent-escapes and
    punycode), so Python's ``lower`` and SQL's agree.
    """
    candidate = raw.strip()
    if not candidate:
        raise ValueError("a muted URL cannot be empty; send an empty list to unmute everything")
    if not _SCHEME.match(candidate):
        candidate = "https://" + candidate.removeprefix("//")
    try:
        url = httpx.URL(normalise_url(candidate))
    except InvalidItemURLError as exc:
        # Not echoing the input: it may be carrying credentials, which is
        # one of the reasons it is being refused.
        raise ValueError(f"not a URL that can be muted: {exc}") from exc

    host = link_host(url)
    if host is None:  # pragma: no cover - normalise_url has refused a hostless URL
        raise ValueError("not a URL that can be muted: no host")
    if host.startswith("www."):
        raise ValueError(f"cannot mute a host that is www. twice over: www.{host}")
    if url.port is not None:
        raise ValueError(f"a muted URL cannot name a port: {host}:{url.port}")

    path = url.raw_path.decode("ascii").split("?", 1)[0]
    segment = path[1:].split("/", 1)[0]
    if not segment and path != "/":
        raise ValueError(f"cannot mute this link on {host}: its first path segment is empty")

    term = (f"{host}/{segment}" if segment else host).lower()
    if len(term) > MAX_MUTED_TERM_LENGTH:
        raise ValueError(
            f"muted URL reduces to {term[:32]!r}…, which is longer than "
            f"{MAX_MUTED_TERM_LENGTH} characters"
        )
    return term


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


def selected_languages(user_id: int) -> Select[tuple[str]]:
    """Codes of the languages this user reads, ordered for stability."""
    return (
        select(UserPreferenceLanguage.language)
        .where(UserPreferenceLanguage.user_id == user_id)
        .order_by(UserPreferenceLanguage.language)
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
        muted_urls=list(db.scalars(muted_terms(user.id, MuteKind.URL)).all()),
        languages=list(db.scalars(selected_languages(user.id)).all()),
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
