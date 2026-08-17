"""Fixtures for the feed, sources, preferences, and user-state suites.

Area-local per AGENTS.md: the root conftest owns app/db/client/authed_client
and is not edited here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.models import FeedItem, FeedItemTopic, Source, SourceTopic, Topic, User

BASE_TIME = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


@dataclass
class Catalogue:
    """Ids of the seeded rows, keyed by the name used in tests."""

    topics: dict[str, int] = field(default_factory=dict)
    sources: dict[str, int] = field(default_factory=dict)
    items: dict[str, int] = field(default_factory=dict)

    @property
    def visible_newest_first(self) -> list[int]:
        """Item ids an unfiltered feed must return, in order.

        ``retired`` is absent: its source is disabled. The two ``bbc``
        items share a publication timestamp, so the tiebreaker decides
        their relative order — descending id, matching the query.
        """
        ordered = [
            self.items["untagged"],
            self.items["lobsters"],
            max(self.items["bbc-a"], self.items["bbc-b"]),
            min(self.items["bbc-a"], self.items["bbc-b"]),
            *[self.items[f"hn-{index}"] for index in reversed(range(6))],
        ]
        return ordered


@pytest.fixture
def catalogue(db_session: Session) -> Catalogue:
    """Topics, sources, and feed items covering the interesting shapes.

    Deliberately included: a disabled source, a disabled topic, a pair of
    items tied on ``published_at``, and an item carrying no topics at all.
    """
    seeded = Catalogue()

    for slug, name, enabled in (
        ("webdev", "Web development", True),
        ("python", "Python", True),
        ("uk-news", "UK news", True),
        ("legacy", "Legacy", False),
    ):
        topic = Topic(slug=slug, name=name, enabled=enabled)
        db_session.add(topic)
        db_session.flush()
        seeded.topics[slug] = topic.id

    for slug, name, enabled in (
        ("hacker-news", "Hacker News", True),
        ("lobsters", "Lobsters", True),
        ("bbc-news", "BBC News", True),
        ("retired", "Retired Source", False),
    ):
        source = Source(
            slug=slug,
            name=name,
            feed_url=f"https://{slug}.example/rss",
            website_url=f"https://{slug}.example",
            enabled=enabled,
        )
        db_session.add(source)
        db_session.flush()
        seeded.sources[slug] = source.id

    db_session.add_all(
        [
            SourceTopic(source_id=seeded.sources["hacker-news"], topic_id=seeded.topics["webdev"]),
            SourceTopic(source_id=seeded.sources["lobsters"], topic_id=seeded.topics["python"]),
            SourceTopic(source_id=seeded.sources["bbc-news"], topic_id=seeded.topics["uk-news"]),
        ]
    )

    plan: list[tuple[str, str, datetime, list[str]]] = [
        *[
            (f"hn-{index}", "hacker-news", BASE_TIME + timedelta(minutes=index), ["webdev"])
            for index in range(6)
        ],
        # Tied publication time: the id tiebreaker is what orders these.
        ("bbc-a", "bbc-news", BASE_TIME + timedelta(minutes=10), ["uk-news"]),
        ("bbc-b", "bbc-news", BASE_TIME + timedelta(minutes=10), ["uk-news"]),
        ("lobsters", "lobsters", BASE_TIME + timedelta(minutes=20), ["python"]),
        # Disabled source: stored, never surfaced by the feed.
        ("retired", "retired", BASE_TIME + timedelta(minutes=30), ["webdev"]),
        # Classified by nothing — the ingest normaliser's failure mode.
        ("untagged", "hacker-news", BASE_TIME + timedelta(minutes=40), []),
    ]
    for key, source_slug, published_at, topic_slugs in plan:
        item = FeedItem(
            source_id=seeded.sources[source_slug],
            canonical_url=f"https://{source_slug}.example/{key}",
            title=f"Story {key}",
            summary=f"Summary for {key}",
            published_at=published_at,
        )
        db_session.add(item)
        db_session.flush()
        seeded.items[key] = item.id
        db_session.add_all(
            FeedItemTopic(feed_item_id=item.id, topic_id=seeded.topics[slug])
            for slug in topic_slugs
        )

    db_session.commit()
    return seeded


@pytest.fixture
def second_user(db_session: Session) -> User:
    """A second account, for the cross-user isolation suite."""
    user = User(github_id=2000002, github_login="hubot", display_name="Hubot")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def sign_in(app: FastAPI, client: TestClient) -> Iterator[Callable[[User], TestClient]]:
    """Return a callable that points the client at a given user.

    Stands in for agent A's cookie session exactly as ``authed_client``
    does, but switchable mid-test — which is what an isolation test needs.
    """

    def _sign_in(user: User) -> TestClient:
        app.dependency_overrides[get_current_user] = lambda: user
        return client

    yield _sign_in
    app.dependency_overrides.pop(get_current_user, None)


@contextmanager
def count_statements(engine: Engine) -> Iterator[list[str]]:
    """Record every SQL statement the engine executes in the block."""
    recorded: list[str] = []

    def _record(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
        recorded.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        yield recorded
    finally:
        event.remove(engine, "before_cursor_execute", _record)
