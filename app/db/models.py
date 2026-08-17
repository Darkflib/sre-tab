"""ORM models for all twelve PRD entities.

Phase 0 property — frozen. No Phase 1 agent edits this file; a schema gap
is escalated, not patched (AGENTS.md).

Contract decisions encoded here:

- Sync SQLAlchemy 2.x by decision; do not convert to asyncio.
- Every relationship is ``lazy="raise"`` so an implicit lazy load in a
  request path fails loudly in tests. Load explicitly (``selectinload``
  or joins).
- Join/state tables use composite primary keys — the compound unique
  constraints that make repeated client requests idempotent.
- ``sessions.token_hash`` stores a SHA-256 hex digest only (see
  ``app.security.tokens``); the raw token never reaches the database.
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


def _values(enum_cls: type[enum.Enum]) -> list[str]:
    return [str(member.value) for member in enum_cls]


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
