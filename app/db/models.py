"""ORM models for the twelve PRD entities, plus ``api_tokens`` and
``user_muted_terms``.

Phase 0 property. The parallel build's rule was that no Phase 1 agent
edits this file and a schema gap is escalated rather than patched
(AGENTS.md); what survives that build is the narrower rule the same
paragraph gives, which is that a revision is never generated *without
meaning to*. ``api_tokens`` and ``user_muted_terms`` are the additions
since — each one class, one revision, added deliberately for a feature
that genuinely needs a table.

Contract decisions encoded here:

- Sync SQLAlchemy 2.x by decision; do not convert to asyncio.
- Every relationship is ``lazy="raise"`` so an implicit lazy load in a
  request path fails loudly in tests. Load explicitly (``selectinload``
  or joins).
- Join/state tables use composite primary keys — the compound unique
  constraints that make repeated client requests idempotent.
- ``sessions.token_hash`` and ``api_tokens.token_hash`` store a SHA-256
  hex digest only (see ``app.security.tokens``); no raw credential ever
  reaches the database.
- Many-to-many convenience relationships are ``viewonly``: writes go
  through the association classes so idempotent upserts stay explicit.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    MetaData,
    String,
    Text,
    false,
    func,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Deterministic constraint names: required for Alembic to emit reversible
# migrations on both SQLite and PostgreSQL.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Theme(enum.StrEnum):
    LIGHT = "light"
    DARK = "dark"
    SYSTEM = "system"


class Layout(enum.StrEnum):
    GRID = "grid"
    LIST = "list"


# Two values rather than a boolean, and not a free string: the
# `Enum(..., native_enum=False)` treatment `Theme` and `Layout` get, plus
# one thing they do not have — `create_constraint=True` on the column, so
# the database itself refuses a value the application does not know and
# adding a third scope is a migration rather than a typo.
#
# That addition is not consistency for its own sake. `create_constraint`
# has defaulted to False since SQLAlchemy 1.4, so `native_enum=False`
# alone yields a bare VARCHAR(16) on both engines: a maintenance script,
# a restore, or a hand-written UPDATE could store 'admin', and the first
# request presenting that token would fail materialising the row with
# `LookupError` — a 500 out of the authentication path, from a column the
# application believed was constrained. On a column that decides what a
# credential may do, "the enum documents the values" is not enough.
#
# `theme` and `layout` do not carry it. They predate this, and adding it
# is a second revision on tables this change has no other reason to
# touch, so it is deliberately not bundled here.
#
# Comments rather than a docstring below the reasoning line, on the
# precedent `app/api/v1/schemas/feed.py` sets for `ReadFilter`: a class
# docstring becomes the component's `description` in the published
# contract, and none of the above is anything a client should be reading.
# The short docstring that remains is written for that audience.
class ApiTokenScope(enum.StrEnum):
    """What an API token may do. `read` permits safe methods only;
    `full` permits everything the owner's account can do."""

    READ = "read"
    FULL = "full"


def _values(enum_cls: type[enum.Enum]) -> list[str]:
    return [str(member.value) for member in enum_cls]


#: Column width for a muted term, and the bound the API validates against.
#: Long enough for a phrase — "premier league football" is twenty-three
#: characters — and short enough that it is plainly a term rather than a
#: paste. The API's own bound is this same number so the two cannot drift
#: into a 500 where a 422 was meant.
MAX_MUTED_TERM_LENGTH = 64


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    # GitHub's stable numeric user ID is the identity anchor — never the
    # login name or email (PRD, Authentication).
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    github_login: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    avatar_url: Mapped[str | None] = mapped_column(String(2048))
    is_admin: Mapped[bool] = mapped_column(default=False, server_default=false(), nullable=False)

    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True, lazy="raise"
    )
    preferences: Mapped[UserPreferences | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True, lazy="raise"
    )
    selected_topics: Mapped[list[Topic]] = relationship(
        secondary="user_preference_topics", viewonly=True, lazy="raise"
    )
    selected_sources: Mapped[list[Source]] = relationship(
        secondary="user_preference_sources", viewonly=True, lazy="raise"
    )


class UserSession(TimestampMixin, Base):
    """PRD entity ``sessions`` (class renamed to avoid clashing with
    :class:`sqlalchemy.orm.Session`)."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="sessions", lazy="raise")


class ApiToken(TimestampMixin, Base):
    """A long-lived per-user credential for calling the API from elsewhere.

    Stored the way ``sessions.token_hash`` is stored, and for the same
    reason: only the SHA-256 hex digest of the raw token is here (see
    ``app.security.tokens``), so a database leak yields nothing that can
    be presented. The raw value exists once, in the creation response.

    ``display_prefix`` is the deliberate exception — a short, non-secret
    leading slice of the token, kept so the owner can tell two tokens
    apart in a list without the server being able to reconstruct either.

    Three timestamps rather than one, because "this token should not be
    here any more" has three different shapes: ``expires_at`` is
    optional, since long-lived is the point; ``revoked_at`` is the user
    saying so; and ``last_used_at`` is what makes a forgotten token
    *visible* rather than merely present. Nothing reads
    ``last_used_at`` to make a decision — it exists so a stale row can
    be recognised as stale.

    No relationship back to :class:`User` is declared. Nothing needs to
    navigate from a token to its owner through the ORM — the resolver
    loads the user by primary key — and the row is removed with the
    account by ``ondelete="CASCADE"``, which is what ``DELETE /me``'s
    single statement relies on.
    """

    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The owner's own name for it. Never a credential, so it is logged
    #: and displayed freely.
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    scope: Mapped[ApiTokenScope] = mapped_column(
        Enum(
            ApiTokenScope,
            name="api_token_scope",
            native_enum=False,
            length=16,
            values_callable=_values,
            # Not the default, and see ApiTokenScope for why it is set
            # here. The constraint's name comes from the convention above
            # — ck_api_tokens_api_token_scope — so the migration can name
            # the same one on the way back down.
            create_constraint=True,
        ),
        nullable=False,
    )
    #: Optional: a token with no expiry is the ordinary case.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserPreferences(TimestampMixin, Base):
    __tablename__ = "user_preferences"
    __table_args__ = (
        CheckConstraint(
            "max_visible_cards >= 1 AND max_visible_cards <= 100", name="max_visible_cards_range"
        ),
    )

    # One profile per user: user_id is both PK and the PRD's unique key.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    theme: Mapped[Theme] = mapped_column(
        Enum(Theme, name="theme", native_enum=False, length=16, values_callable=_values),
        default=Theme.SYSTEM,
        server_default=Theme.SYSTEM.value,
        nullable=False,
    )
    layout: Mapped[Layout] = mapped_column(
        Enum(Layout, name="layout", native_enum=False, length=16, values_callable=_values),
        default=Layout.GRID,
        server_default=Layout.GRID.value,
        nullable=False,
    )
    max_visible_cards: Mapped[int] = mapped_column(default=25, server_default="25", nullable=False)
    onboarding_completed: Mapped[bool] = mapped_column(
        default=False, server_default=false(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="preferences", lazy="raise")


class MuteKind(enum.StrEnum):
    """What a muted term is matched against."""

    #: Matched against the item's title and summary.
    WORD = "word"
    #: Matched against the item's topic slugs.
    TAG = "tag"


class UserMutedTerm(Base):
    """A term whose items this user does not want to see.

    A table rather than a JSON column on ``user_preferences``, and for the
    reason every other collection here is one: the predicate that uses it
    runs in SQL, inside the feed's own statement, and a JSON array cannot
    be joined or indexed without the database growing an opinion about
    JSON that differs between the two engines this project supports.

    The primary key is ``(user_id, kind, term)``, which is the same
    composite-key idempotence ``user_preference_topics`` has: muting a
    term twice is one row, so a client that retries a save cannot create
    duplicates.

    ``kind`` carries ``create_constraint=True`` for the reason
    :class:`ApiTokenScope` records — ``Enum(native_enum=False)`` renders
    as a bare ``VARCHAR`` and emits no constraint of its own, so without
    it a restore or a hand-written ``UPDATE`` could store a kind the
    application does not know, and the row would fail to materialise with
    ``LookupError`` on the next feed request.

    Terms are stored normalised — case-folded and stripped by
    ``app.services.preferences`` before they arrive — so the uniqueness
    the primary key promises is uniqueness a reader would recognise
    rather than uniqueness of bytes. A tag term is a topic slug and is
    validated against the catalogue at write time; a word term is free
    text and is deliberately not validated against anything, because the
    whole point is muting language the catalogue has never heard of.
    """

    __tablename__ = "user_muted_terms"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[MuteKind] = mapped_column(
        Enum(
            MuteKind,
            name="mute_kind",
            native_enum=False,
            length=16,
            values_callable=_values,
            create_constraint=True,
        ),
        primary_key=True,
    )
    term: Mapped[str] = mapped_column(String(MAX_MUTED_TERM_LENGTH), primary_key=True)


class UserPreferenceTopic(Base):
    __tablename__ = "user_preference_topics"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    topic_id: Mapped[int] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True
    )


class UserPreferenceSource(Base):
    __tablename__ = "user_preference_sources"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, server_default=true(), nullable=False)


class Source(TimestampMixin, Base):
    __tablename__ = "sources"
    __table_args__ = (CheckConstraint("refresh_minutes >= 1", name="refresh_minutes_positive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    feed_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    website_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    # icon_url is in the functional requirements ("optional icon") though
    # not in the PRD data-model table.
    icon_url: Mapped[str | None] = mapped_column(String(2048))
    refresh_minutes: Mapped[int] = mapped_column(default=30, server_default="30", nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, server_default=true(), nullable=False)

    topics: Mapped[list[Topic]] = relationship(
        secondary="source_topics", viewonly=True, lazy="raise"
    )


class SourceStatus(Base):
    """Scheduler-written refresh state for one source.

    A separate table rather than columns on ``sources``, on purpose:
    ``sources`` is operator-managed configuration and this is runtime
    state written by the refresh loop. Keeping them apart means the two
    writers never contend, and ``sources.updated_at`` keeps meaning "the
    operator changed the configuration" rather than "a feed was polled".

    1:1 with ``sources`` — ``source_id`` is both primary key and foreign
    key — and it cascades, so retiring a source takes its status with it.

    Persisting ``last_fetched_at`` is also what stops the refresh
    schedule living only in one process's memory: a restart or a second
    replica reads when the source was last attempted instead of treating
    every source as due immediately.
    """

    __tablename__ = "source_status"

    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    #: Last attempt, successful or not.
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_class: Mapped[str | None] = mapped_column(String(64))
    last_error_detail: Mapped[str | None] = mapped_column(String(500))
    consecutive_failures: Mapped[int] = mapped_column(default=0, server_default="0", nullable=False)


class SourceTopic(Base):
    __tablename__ = "source_topics"

    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    topic_id: Mapped[int] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True
    )


class FeedItem(Base):
    __tablename__ = "feed_items"
    # Composite index backing published_at-ordered cursor pagination.
    __table_args__ = (Index("ix_feed_items_published_at_id", "published_at", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    canonical_url: Mapped[str] = mapped_column(String(2048), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    # Not nullable: the normaliser falls back to fetch time when a feed
    # omits a publication date.
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    image_url: Mapped[str | None] = mapped_column(String(2048))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    source: Mapped[Source] = relationship(lazy="raise")
    topics: Mapped[list[Topic]] = relationship(
        secondary="feed_item_topics", viewonly=True, lazy="raise"
    )


class FeedItemTopic(Base):
    __tablename__ = "feed_item_topics"

    feed_item_id: Mapped[int] = mapped_column(
        ForeignKey("feed_items.id", ondelete="CASCADE"), primary_key=True
    )
    topic_id: Mapped[int] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True
    )


class UserReadItem(Base):
    __tablename__ = "user_read_items"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    feed_item_id: Mapped[int] = mapped_column(
        ForeignKey("feed_items.id", ondelete="CASCADE"), primary_key=True
    )
    read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    item: Mapped[FeedItem] = relationship(lazy="raise")


class Bookmark(Base):
    __tablename__ = "bookmarks"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    feed_item_id: Mapped[int] = mapped_column(
        ForeignKey("feed_items.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    item: Mapped[FeedItem] = relationship(lazy="raise")
