"""PostgreSQL branches of the write paths, and the migration itself.

``upsert_items``, ``persist_source_status``, and ``insert_ignore`` each
carry a PostgreSQL branch and a SQLite branch. The ordinary suite only
ever exercises the SQLite one, so a PostgreSQL-only mistake would ship
and surface in production. These tests run the same code against the
engine it will actually meet.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.db.models import Bookmark, FeedItem, Source, Topic, User
from app.db.models import SourceStatus as SourceStatusRow
from app.ingest.normalise import NormalisedItem
from app.ingest.status import SourceStatusRegistry
from app.ingest.store import prune_feed_items, upsert_items
from tests.postgres.conftest import postgres_url
from tests.postgres.conftest import pytestmark as _pytestmark

pytestmark = _pytestmark

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PRE_PHASE_2 = "d25a61924953"
#: The revision before ``api_tokens``, so the token migration can be
#: stepped over on its own rather than only as half of a two-revision
#: downgrade, where a mistake in either could be masked by the other.
PRE_API_TOKENS = "29038199b328"
NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def _item(url: str, *, published: datetime | None = None) -> NormalisedItem:
    return NormalisedItem(
        canonical_url=url,
        title="Title",
        summary="Summary",
        published_at=published or NOW,
        image_url=None,
    )


@pytest.fixture
def pg_source(pg_session: Session) -> Source:
    source = Source(
        slug="lobsters",
        name="Lobsters",
        feed_url="https://lobste.rs/rss",
        website_url="https://lobste.rs/",
        refresh_minutes=30,
    )
    pg_session.add(source)
    pg_session.commit()
    pg_session.refresh(source)
    return source


# --- ingest writes ------------------------------------------------------


def test_upsert_is_idempotent_on_postgres(pg_session: Session, pg_source: Source) -> None:
    items = [_item("https://example.org/a"), _item("https://example.org/b")]
    assert (
        upsert_items(pg_session, source_id=pg_source.id, items=items, topic_ids=[], fetched_at=NOW)
        == 2
    )
    assert (
        upsert_items(pg_session, source_id=pg_source.id, items=items, topic_ids=[], fetched_at=NOW)
        == 0
    )
    pg_session.commit()
    assert len(pg_session.scalars(select(FeedItem)).all()) == 2


def test_topic_links_use_the_postgres_conflict_branch(
    pg_session: Session, pg_source: Source
) -> None:
    topic = Topic(slug="open-source", name="Open source")
    pg_session.add(topic)
    pg_session.commit()

    items = [_item("https://example.org/a")]
    for _ in range(3):
        upsert_items(
            pg_session,
            source_id=pg_source.id,
            items=items,
            topic_ids=[topic.id],
            fetched_at=NOW,
        )
    pg_session.commit()
    assert len(pg_session.scalars(select(FeedItem.id)).all()) == 1


def test_prune_spares_bookmarked_items_on_postgres(pg_session: Session, pg_source: Source) -> None:
    """The NOT EXISTS predicate, on the engine that will run it."""
    user = User(github_id=101405, github_login="darkflib")
    pg_session.add(user)
    pg_session.commit()

    cutoff = NOW - timedelta(days=90)
    ancient = cutoff - timedelta(days=30)
    upsert_items(
        pg_session,
        source_id=pg_source.id,
        items=[
            _item("https://example.org/saved", published=ancient),
            _item("https://example.org/unsaved", published=ancient),
        ],
        topic_ids=[],
        fetched_at=NOW,
    )
    saved = pg_session.scalars(
        select(FeedItem).where(FeedItem.canonical_url == "https://example.org/saved")
    ).one()
    pg_session.add(Bookmark(user_id=user.id, feed_item_id=saved.id))
    pg_session.commit()

    assert prune_feed_items(pg_session, cutoff=cutoff) == 1
    pg_session.commit()
    assert set(pg_session.scalars(select(FeedItem.canonical_url))) == {"https://example.org/saved"}


# --- status write-through -----------------------------------------------


def test_status_upsert_updates_in_place_on_postgres(
    pg_session_factory: sessionmaker[Session], pg_session: Session, pg_source: Source
) -> None:
    registry = SourceStatusRegistry(pg_session_factory)
    registry.record_failure(
        source_id=pg_source.id,
        slug=pg_source.slug,
        refresh_minutes=30,
        error_class="FetchError",
        detail="down",
        now=NOW,
    )
    registry.record_success(
        source_id=pg_source.id,
        slug=pg_source.slug,
        refresh_minutes=30,
        item_count=3,
        inserted_count=3,
        now=NOW + timedelta(minutes=30),
    )

    pg_session.expire_all()
    rows = pg_session.scalars(select(SourceStatusRow)).all()
    assert len(rows) == 1
    assert rows[0].consecutive_failures == 0
    assert rows[0].last_error_class is None


def test_a_cold_registry_reads_the_persisted_schedule_on_postgres(
    pg_session_factory: sessionmaker[Session], pg_source: Source
) -> None:
    SourceStatusRegistry(pg_session_factory).record_success(
        source_id=pg_source.id,
        slug=pg_source.slug,
        refresh_minutes=30,
        item_count=1,
        inserted_count=1,
        now=NOW,
    )
    cold = SourceStatusRegistry(pg_session_factory)
    assert cold.is_due(pg_source.id, refresh_minutes=30, now=NOW + timedelta(minutes=5)) is False
    assert cold.is_due(pg_source.id, refresh_minutes=30, now=NOW + timedelta(minutes=31)) is True


# --- migrations ---------------------------------------------------------


@pytest.fixture
def pg_alembic_config(pg_engine: Engine) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", postgres_url())
    return config


def test_migrations_round_trip_on_a_populated_postgres(
    pg_alembic_config: Config, pg_engine: Engine
) -> None:
    """Fresh database, seeded at the previous revision, then upgraded.

    The schema is dropped and rebuilt by Alembic here rather than by
    ``create_all``, so the assertion is about the migration and not about
    the models.
    """
    from app.db.models import Base

    Base.metadata.drop_all(pg_engine)
    with pg_engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))

    try:
        command.upgrade(pg_alembic_config, PRE_PHASE_2)
        with pg_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, github_id, github_login) VALUES (1, 101405, 'darkflib')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO sources (id, slug, name, feed_url, website_url) "
                    "VALUES (1, 'lobsters', 'Lobsters', "
                    "'https://lobste.rs/rss', 'https://lobste.rs/')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO feed_items (id, source_id, canonical_url, title, published_at) "
                    "VALUES (1, 1, 'https://example.org/a', 'An article', now())"
                )
            )
            connection.execute(text("INSERT INTO bookmarks (user_id, feed_item_id) VALUES (1, 1)"))

        command.upgrade(pg_alembic_config, "head")
        tables = inspect(pg_engine).get_table_names()
        assert "source_status" in tables
        assert "api_tokens" in tables
        with pg_engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM bookmarks")).scalar_one() == 1

        with pg_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO source_status (source_id, last_fetched_at, "
                    "consecutive_failures) VALUES (1, now(), 0)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO api_tokens "
                    "(user_id, label, token_hash, display_prefix, scope) "
                    f"VALUES (1, 'laptop', '{'a' * 64}', 'sretab_pat_aaaaaa', 'read')"
                )
            )
        with pg_engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM api_tokens")).scalar_one() == 1

        # One revision at a time on the way down, so a defect in either
        # downgrade cannot be hidden by the other having worked.
        command.downgrade(pg_alembic_config, PRE_API_TOKENS)
        tables = inspect(pg_engine).get_table_names()
        assert "api_tokens" not in tables
        assert "source_status" in tables

        command.downgrade(pg_alembic_config, PRE_PHASE_2)
        assert "source_status" not in inspect(pg_engine).get_table_names()
        with pg_engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM users")).scalar_one() == 1

        command.downgrade(pg_alembic_config, "base")
        assert inspect(pg_engine).get_table_names() == ["alembic_version"]
    finally:
        # Leave the schema as the session fixture expects to find it.
        with pg_engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        Base.metadata.create_all(pg_engine)


def test_the_scope_column_refuses_an_unknown_value_on_postgres(
    pg_clean: Engine, pg_session: Session
) -> None:
    """The CHECK constraint on ``api_tokens.scope``, on the engine that
    will meet the restore.

    ``Enum(native_enum=False)`` emits no constraint of its own —
    ``create_constraint`` has defaulted to False since SQLAlchemy 1.4 —
    so without it this column is a bare ``VARCHAR(16)`` and an unknown
    scope reaching the table would surface as a ``LookupError`` on the
    next request that presented the token. SQLite is checked the same way
    in ``tests/test_migrations.py``; the two dialects render CHECK
    differently often enough to be worth asking both.

    The valid insert first, so a table that refused every write could not
    pass this.
    """
    user = User(github_id=101405, github_login="darkflib")
    pg_session.add(user)
    pg_session.commit()

    with pg_clean.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO api_tokens "
                "(user_id, label, token_hash, display_prefix, scope) "
                f"VALUES ({user.id}, 'laptop', '{'a' * 64}', 'sretab_pat_aaaaaa', 'read')"
            )
        )

    with pytest.raises(IntegrityError), pg_clean.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO api_tokens "
                "(user_id, label, token_hash, display_prefix, scope) "
                f"VALUES ({user.id}, 'escalation', '{'b' * 64}', 'sretab_pat_bbbbbb', 'admin')"
            )
        )

    with pg_clean.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM api_tokens")).scalar_one() == 1
