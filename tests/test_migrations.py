"""Migrations upgrade and downgrade cleanly, empty and populated.

The populated case is a PRD non-functional target rather than a nicety:
a revision that only ever runs against an empty database is untested for
the one situation it will actually meet.

PostgreSQL is exercised the same way by ``deploy/scripts/check-migrations.sh``,
which CI runs against a real server. This suite is SQLite, so it stays in
the ordinary ``pytest`` gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.db.engine import create_db_engine

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The revision before Phase 2 added ``source_status``. Upgrading to this,
#: seeding it, and then moving to head is what makes the populated test a
#: real migration rather than a fresh create_all.
PRE_PHASE_2 = "d25a61924953"

#: The revision before ``api_tokens``. Named separately so the token
#: migration can be stepped over on its own rather than only as part of a
#: two-revision downgrade, where a mistake in one could be hidden by the
#: other.
PRE_API_TOKENS = "29038199b328"

ENTITY_TABLES = {
    "users",
    "sessions",
    "user_preferences",
    "user_preference_topics",
    "user_preference_sources",
    "topics",
    "sources",
    "source_topics",
    "feed_items",
    "feed_item_topics",
    "user_read_items",
    "bookmarks",
    # Phase 2: scheduler-written refresh state, separate from the
    # operator-managed sources row.
    "source_status",
    # Long-lived per-user API credentials.
    "api_tokens",
    # Words and tags a user does not want to see.
    "user_muted_terms",
}


@pytest.fixture
def alembic_config(tmp_path: Path) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path / 'migrate.db'}")
    return config


@pytest.fixture
def migrate_engine(alembic_config: Config) -> Engine:
    url = alembic_config.get_main_option("sqlalchemy.url")
    assert url is not None
    # create_db_engine rather than create_engine: it turns SQLite's
    # foreign keys on, which the cascade assertion below depends on.
    return create_db_engine(url)


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _count(engine: Engine, table: str) -> int:
    # Table names are literals from this module, never user input.
    with engine.connect() as connection:
        return int(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


def _seed(engine: Engine) -> None:
    """Representative rows, written as SQL against the *old* schema.

    Raw SQL rather than the ORM on purpose: the models describe head, and
    seeding a historical revision through them would quietly test the
    wrong schema.
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, github_id, github_login, display_name) "
                "VALUES (1, 101405, 'darkflib', 'Mike Preston')"
            )
        )
        connection.execute(text("INSERT INTO user_preferences (user_id) VALUES (1)"))
        connection.execute(
            text("INSERT INTO topics (id, slug, name) VALUES (1, 'devops', 'DevOps')")
        )
        connection.execute(
            text(
                "INSERT INTO sources (id, slug, name, feed_url, website_url, refresh_minutes) "
                "VALUES (1, 'lobsters', 'Lobsters', "
                "'https://lobste.rs/rss', 'https://lobste.rs/', 30)"
            )
        )
        connection.execute(text("INSERT INTO source_topics (source_id, topic_id) VALUES (1, 1)"))
        connection.execute(
            text("INSERT INTO user_preference_topics (user_id, topic_id) VALUES (1, 1)")
        )
        connection.execute(
            text("INSERT INTO user_preference_sources (user_id, source_id) VALUES (1, 1)")
        )
        connection.execute(
            text(
                "INSERT INTO feed_items "
                "(id, source_id, canonical_url, title, published_at) "
                "VALUES (1, 1, 'https://example.org/a', 'An article', '2026-08-01 09:00:00')"
            )
        )
        connection.execute(
            text("INSERT INTO feed_item_topics (feed_item_id, topic_id) VALUES (1, 1)")
        )
        connection.execute(
            text("INSERT INTO user_read_items (user_id, feed_item_id) VALUES (1, 1)")
        )
        connection.execute(text("INSERT INTO bookmarks (user_id, feed_item_id) VALUES (1, 1)"))


def test_exactly_one_head(alembic_config: Config) -> None:
    """One head, always. A fork is what a second concurrent revision
    would produce, and it is the failure this assertion exists to catch."""
    script = ScriptDirectory.from_config(alembic_config)
    assert len(script.get_heads()) == 1


def test_upgrade_head_and_downgrade_base(alembic_config: Config, migrate_engine: Engine) -> None:
    command.upgrade(alembic_config, "head")
    assert _tables(migrate_engine) == ENTITY_TABLES | {"alembic_version"}

    command.downgrade(alembic_config, "base")
    assert _tables(migrate_engine) == {"alembic_version"}


def test_upgrade_and_downgrade_against_a_populated_database(
    alembic_config: Config, migrate_engine: Engine
) -> None:
    command.upgrade(alembic_config, PRE_PHASE_2)
    _seed(migrate_engine)

    command.upgrade(alembic_config, "head")

    # Nothing seeded was disturbed by the new table.
    for table in ("users", "sources", "topics", "feed_items", "bookmarks", "user_read_items"):
        assert _count(migrate_engine, table) == 1
    assert _count(migrate_engine, "source_status") == 0

    with migrate_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO source_status "
                "(source_id, last_fetched_at, consecutive_failures) "
                "VALUES (1, '2026-08-17 12:00:00', 0)"
            )
        )
    assert _count(migrate_engine, "source_status") == 1

    _seed_api_token(migrate_engine)
    assert _count(migrate_engine, "api_tokens") == 1

    # One revision at a time on the way back down. Collapsing both into a
    # single downgrade would let a mistake in either be masked by the
    # other — the tables would still all be gone at the end.
    command.downgrade(alembic_config, PRE_API_TOKENS)
    assert "api_tokens" not in _tables(migrate_engine)
    assert "source_status" in _tables(migrate_engine)
    assert _count(migrate_engine, "source_status") == 1

    command.downgrade(alembic_config, PRE_PHASE_2)
    assert "source_status" not in _tables(migrate_engine)
    # The downgrade drops the new table and touches nothing else.
    for table in ("users", "sources", "feed_items", "bookmarks"):
        assert _count(migrate_engine, table) == 1

    command.downgrade(alembic_config, "base")
    assert _tables(migrate_engine) == {"alembic_version"}


def _seed_api_token(engine: Engine) -> None:
    """One token row, written as SQL against the schema at head."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO api_tokens "
                "(id, user_id, label, token_hash, display_prefix, scope) "
                f"VALUES (1, 1, 'laptop', '{'a' * 64}', 'sretab_pat_aaaaaa', 'read')"
            )
        )


def test_the_scope_column_refuses_a_value_the_application_does_not_know(
    alembic_config: Config, migrate_engine: Engine
) -> None:
    """``Enum(native_enum=False)`` emits no CHECK constraint on its own.

    ``create_constraint`` has defaulted to False since SQLAlchemy 1.4, so
    the obvious spelling leaves a bare ``VARCHAR(16)`` and "the database
    refuses an unknown scope" would be prose rather than a property. This
    asks the *migrated* schema, not the models: a restore or a
    hand-written ``UPDATE`` meets the database, and an unknown value
    stored there fails when SQLAlchemy materialises the row — a
    ``LookupError`` out of the authentication path, which is a 500 rather
    than a refusal.

    The valid insert above it is not decoration. Without it the refusal
    would pass just as happily against a table that rejects every write.
    """
    command.upgrade(alembic_config, "head")
    with migrate_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO users (id, github_id, github_login) VALUES (1, 101405, 'darkflib')")
        )
    _seed_api_token(migrate_engine)

    with pytest.raises(IntegrityError), migrate_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO api_tokens "
                "(id, user_id, label, token_hash, display_prefix, scope) "
                f"VALUES (2, 1, 'escalation', '{'b' * 64}', 'sretab_pat_bbbbbb', 'admin')"
            )
        )

    assert _count(migrate_engine, "api_tokens") == 1


def test_api_tokens_cascade_with_their_owner(
    alembic_config: Config, migrate_engine: Engine
) -> None:
    """``DELETE /me`` is one statement leaning on this cascade, so the
    schema is where it has to be true — not the route."""
    command.upgrade(alembic_config, "head")
    with migrate_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO users (id, github_id, github_login) VALUES (1, 101405, 'darkflib')")
        )
    _seed_api_token(migrate_engine)
    assert _count(migrate_engine, "api_tokens") == 1

    with migrate_engine.begin() as connection:
        connection.execute(text("DELETE FROM users WHERE id = 1"))

    assert _count(migrate_engine, "api_tokens") == 0


def test_source_status_cascades_with_its_source(
    alembic_config: Config, migrate_engine: Engine
) -> None:
    """Retiring a source takes its refresh state with it."""
    command.upgrade(alembic_config, "head")
    with migrate_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sources (id, slug, name, feed_url, website_url) "
                "VALUES (1, 'lwn', 'LWN', "
                "'https://lwn.net/headlines/newrss', 'https://lwn.net/')"
            )
        )
        connection.execute(text("INSERT INTO source_status (source_id) VALUES (1)"))
    with migrate_engine.begin() as connection:
        connection.execute(text("DELETE FROM sources WHERE id = 1"))
    assert _count(migrate_engine, "source_status") == 0
