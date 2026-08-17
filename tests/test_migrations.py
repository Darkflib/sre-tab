"""The single initial revision upgrades and downgrades cleanly on SQLite."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from alembic import command

REPO_ROOT = Path(__file__).resolve().parent.parent

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
}


@pytest.fixture
def alembic_config(tmp_path: Path) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path / 'migrate.db'}")
    return config


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_exactly_one_head_and_one_revision(alembic_config: Config) -> None:
    script = ScriptDirectory.from_config(alembic_config)
    assert len(script.get_heads()) == 1
    assert len(list(script.walk_revisions())) == 1


def test_upgrade_head_and_downgrade_base(alembic_config: Config) -> None:
    url = alembic_config.get_main_option("sqlalchemy.url")
    assert url is not None

    command.upgrade(alembic_config, "head")
    assert _tables(url) == ENTITY_TABLES | {"alembic_version"}

    command.downgrade(alembic_config, "base")
    assert _tables(url) == {"alembic_version"}
