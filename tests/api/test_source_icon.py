"""Which icon a card carries, and what it costs to say.

`sources.icon_url` is the operator's answer and
`source_status.discovered_icon_url` is what the last successful parse
found in the channel. The API coalesces them, in that order, and the two
are kept in separate tables on purpose — see `SourceStatus` in
`app/db/models.py`. What is tested here is the coalescing, that neither
write erases the other, and that reaching the second one costs no extra
statement.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.db.models import Bookmark, Source, SourceStatus, User
from tests.api.conftest import Catalogue, count_statements


@pytest.fixture
def hn(db_session: Session, catalogue: Catalogue) -> Source:
    return db_session.scalars(select(Source).where(Source.slug == "hacker-news")).one()


def _card(client: TestClient, catalogue: Catalogue) -> dict[str, Any]:
    payload = client.get("/api/v1/feed", params={"limit": 100}).json()
    return next(item for item in payload["items"] if item["id"] == catalogue.items["hn-0"])


def test_no_icon_anywhere_is_null(
    authed_client: TestClient, catalogue: Catalogue, hn: Source
) -> None:
    assert _card(authed_client, catalogue)["source"]["icon_url"] is None


def test_the_discovered_icon_is_used_when_the_operator_set_none(
    authed_client: TestClient, catalogue: Catalogue, hn: Source, db_session: Session
) -> None:
    """The case that closes the roadmap's "sources render an icon and no
    source has one": nothing sets `icon_url`, so before this the affordance
    was built and never fired."""
    db_session.add(SourceStatus(source_id=hn.id, discovered_icon_url="https://cdn/found.png"))
    db_session.commit()

    assert _card(authed_client, catalogue)["source"]["icon_url"] == "https://cdn/found.png"


def test_the_operator_icon_wins_over_the_discovered_one(
    authed_client: TestClient, catalogue: Catalogue, hn: Source, db_session: Session
) -> None:
    """Configuration beats discovery. An operator who sets an icon must not
    have it replaced by whatever the publisher ships next week."""
    hn.icon_url = "https://cdn/operator.png"
    db_session.add(SourceStatus(source_id=hn.id, discovered_icon_url="https://cdn/found.png"))
    db_session.commit()

    assert _card(authed_client, catalogue)["source"]["icon_url"] == "https://cdn/operator.png"


def test_a_source_never_polled_still_returns_its_items(
    authed_client: TestClient, catalogue: Catalogue
) -> None:
    """`source_status` is 1:1 and written only once a source has been
    polled, so the join to it has to be outer. An inner join would drop
    every item from a source whose first refresh has not finished — a feed
    that silently loses a publication rather than an icon."""
    payload = authed_client.get("/api/v1/feed", params={"limit": 100}).json()

    assert len(payload["items"]) == len(catalogue.visible_newest_first)


def test_bookmarks_carry_the_same_icon(
    authed_client: TestClient,
    catalogue: Catalogue,
    hn: Source,
    db_session: Session,
    test_user: User,
) -> None:
    """`build_item_out` is shared, so the join has to be too — a bookmark
    card missing the icon its feed card has would be a difference nobody
    designed."""
    db_session.add(SourceStatus(source_id=hn.id, discovered_icon_url="https://cdn/found.png"))
    db_session.add(Bookmark(user_id=test_user.id, feed_item_id=catalogue.items["hn-0"]))
    db_session.commit()

    payload = authed_client.get("/api/v1/bookmarks").json()

    assert payload["bookmarks"][0]["item"]["source"]["icon_url"] == "https://cdn/found.png"


def test_reaching_the_discovered_icon_costs_no_extra_statement(
    authed_client: TestClient, catalogue: Catalogue, hn: Source, db_session: Session, engine: Engine
) -> None:
    """It rides the join the source already needs. A `selectinload` would
    have been one more statement per page, and a lazy load one per card —
    which `lazy="raise"` would turn into a failure rather than a
    slowdown, but only if a test happened to look."""
    db_session.add(SourceStatus(source_id=hn.id, discovered_icon_url="https://cdn/found.png"))
    db_session.commit()

    with count_statements(engine) as recorded:
        authed_client.get("/api/v1/feed", params={"limit": 100})

    assert sum("feed_items" in sql for sql in recorded) == 2
    assert not any("source_status" in sql and "feed_items" not in sql for sql in recorded)


def test_the_icon_survives_the_status_row_being_rewritten(
    authed_client: TestClient, catalogue: Catalogue, hn: Source, db_session: Session
) -> None:
    """Two writers share the row: the icon rides the item write and the
    timings ride the status registry. Neither may blank the other's
    column."""
    from datetime import UTC, datetime

    from app.ingest.status import persist_source_status

    db_session.add(SourceStatus(source_id=hn.id, discovered_icon_url="https://cdn/found.png"))
    db_session.commit()

    persist_source_status(
        db_session,
        source_id=hn.id,
        last_fetched_at=datetime(2026, 9, 3, tzinfo=UTC),
        last_success_at=datetime(2026, 9, 3, tzinfo=UTC),
        last_error_class=None,
        last_error_detail=None,
        consecutive_failures=0,
    )
    db_session.commit()

    assert _card(authed_client, catalogue)["source"]["icon_url"] == "https://cdn/found.png"


def test_an_item_from_a_polled_source_is_not_duplicated(
    authed_client: TestClient, catalogue: Catalogue, hn: Source, db_session: Session
) -> None:
    """`source_status` is 1:1, so the outer join cannot multiply rows —
    asserted rather than assumed, because a join that did would show up as
    repeated cards under a LIMIT and nothing else in this suite looks."""
    db_session.add(SourceStatus(source_id=hn.id, discovered_icon_url="https://cdn/found.png"))
    db_session.commit()

    ids = [
        item["id"]
        for item in authed_client.get("/api/v1/feed", params={"limit": 100}).json()["items"]
    ]

    assert ids == catalogue.visible_newest_first
    assert len(ids) == len(set(ids))
