"""``sre-tab retag`` — the pass that makes the URL ruleset correctable.

Ingest derives an item's topics once, when the item arrives, so a change
to the ruleset describes nothing already stored. This is what brings the
retained window into line with it, and the property under test throughout
is the boundary: it owns ``origin='rule'`` and may not touch anything
else.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.cli import main
from app.cli import operations as ops
from app.db.engine import create_db_engine
from app.db.models import FeedItem, FeedItemTopic, Source, Topic, TopicOrigin
from app.db.session import build_session_factory

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)

SPORT_URL = "https://www.bbc.co.uk/sport/cricket/videos/cq70dnxrg79lo"
NEWS_URL = "https://www.bbc.co.uk/news/articles/cx980qj57r89o"


@pytest.fixture
def corpus(db_session: Session) -> Session:
    """Two items and two topics, with only the source's links written.

    Deliberately *not* written through ``upsert_items``: the whole reason
    this command exists is items that were stored before the ruleset knew
    anything about them.
    """
    source = Source(
        slug="bbc-news",
        name="BBC News",
        feed_url="https://feeds.bbci.co.uk/news/rss.xml",
        website_url="https://www.bbc.co.uk/news",
        refresh_minutes=15,
    )
    db_session.add(source)
    uk = Topic(slug="uk-news", name="UK news")
    sport = Topic(slug="sport", name="Sport")
    db_session.add_all([uk, sport])
    db_session.flush()

    for url in (SPORT_URL, NEWS_URL):
        row = FeedItem(
            source_id=source.id,
            canonical_url=url,
            title="Title",
            published_at=NOW,
            fetched_at=NOW,
        )
        db_session.add(row)
        db_session.flush()
        db_session.add(
            FeedItemTopic(feed_item_id=row.id, topic_id=uk.id, origin=TopicOrigin.SOURCE)
        )
    db_session.commit()
    return db_session


def links(session: Session, url: str) -> dict[str, TopicOrigin]:
    rows = session.execute(
        select(Topic.slug, FeedItemTopic.origin)
        .join(FeedItemTopic, FeedItemTopic.topic_id == Topic.id)
        .join(FeedItem, FeedItem.id == FeedItemTopic.feed_item_id)
        .where(FeedItem.canonical_url == url)
    ).all()
    return dict(rows)  # type: ignore[arg-type]


def test_it_backfills_the_window_the_ruleset_never_saw(corpus: Session) -> None:
    report = ops.retag_items(corpus)
    corpus.commit()

    assert report.items_examined == 2
    assert report.links_added == 1
    assert report.links_removed == 0
    assert links(corpus, SPORT_URL) == {
        "uk-news": TopicOrigin.SOURCE,
        "sport": TopicOrigin.RULE,
    }
    assert links(corpus, NEWS_URL) == {"uk-news": TopicOrigin.SOURCE}


def test_running_it_twice_changes_nothing(corpus: Session) -> None:
    ops.retag_items(corpus)
    corpus.commit()

    report = ops.retag_items(corpus)
    corpus.commit()

    assert report.changed is False
    assert report.links_added == 0
    assert report.links_removed == 0


def test_a_rule_link_the_ruleset_no_longer_derives_is_removed(corpus: Session) -> None:
    """The correction path: a previous version of the ruleset put ``sport``
    on an item whose URL says nothing of the kind."""
    news = corpus.scalars(select(FeedItem).where(FeedItem.canonical_url == NEWS_URL)).one()
    sport = corpus.scalars(select(Topic).where(Topic.slug == "sport")).one()
    corpus.add(FeedItemTopic(feed_item_id=news.id, topic_id=sport.id, origin=TopicOrigin.RULE))
    corpus.commit()

    report = ops.retag_items(corpus)
    corpus.commit()

    assert report.links_removed == 1
    assert links(corpus, NEWS_URL) == {"uk-news": TopicOrigin.SOURCE}


def test_it_never_removes_a_link_the_source_asserted(corpus: Session) -> None:
    """The same wrong-looking link, but asserted by the operator. It must
    survive, because the ruleset does not own it — that boundary is the
    entire reason ``origin`` exists."""
    news = corpus.scalars(select(FeedItem).where(FeedItem.canonical_url == NEWS_URL)).one()
    sport = corpus.scalars(select(Topic).where(Topic.slug == "sport")).one()
    corpus.add(FeedItemTopic(feed_item_id=news.id, topic_id=sport.id, origin=TopicOrigin.SOURCE))
    corpus.commit()

    report = ops.retag_items(corpus)
    corpus.commit()

    assert report.links_removed == 0
    assert links(corpus, NEWS_URL) == {
        "uk-news": TopicOrigin.SOURCE,
        "sport": TopicOrigin.SOURCE,
    }


def test_a_pair_the_source_already_asserts_is_not_proposed_again(corpus: Session) -> None:
    """``sport`` on the sport item, but from the source. The ruleset would
    derive the same pair; it must report nothing to do rather than
    counting an insert that conflicts away."""
    item = corpus.scalars(select(FeedItem).where(FeedItem.canonical_url == SPORT_URL)).one()
    sport = corpus.scalars(select(Topic).where(Topic.slug == "sport")).one()
    corpus.add(FeedItemTopic(feed_item_id=item.id, topic_id=sport.id, origin=TopicOrigin.SOURCE))
    corpus.commit()

    report = ops.retag_items(corpus)
    corpus.commit()

    assert report.changed is False
    assert links(corpus, SPORT_URL) == {
        "uk-news": TopicOrigin.SOURCE,
        "sport": TopicOrigin.SOURCE,
    }


def test_a_slug_the_instance_has_no_topic_row_for_is_a_missing_tag_not_a_failure(
    corpus: Session,
) -> None:
    sport_id = corpus.scalars(select(Topic.id).where(Topic.slug == "sport")).one()
    corpus.execute(delete(FeedItemTopic).where(FeedItemTopic.topic_id == sport_id))
    corpus.execute(delete(Topic).where(Topic.slug == "sport"))
    corpus.commit()

    report = ops.retag_items(corpus)
    corpus.commit()

    assert report.changed is False
    assert links(corpus, SPORT_URL) == {"uk-news": TopicOrigin.SOURCE}


def test_dry_run_reports_the_same_counts_and_writes_nothing(corpus: Session) -> None:
    report = ops.retag_items(corpus, dry_run=True)
    corpus.commit()

    assert report.links_added == 1
    assert links(corpus, SPORT_URL) == {"uk-news": TopicOrigin.SOURCE}

    applied = ops.retag_items(corpus)
    corpus.commit()
    assert applied.links_added == report.links_added


# --- the command ---------------------------------------------------------


@pytest.fixture
def seeded_db(tmp_path: pathlib.Path) -> str:
    """A migrated file database holding the seed catalogue and one item
    from a section the ruleset maps.

    A file database rather than the shared in-memory one, so the CLI's own
    session handling and commit are what write — the reasoning
    ``tests/cli/test_operations.py`` gives for ``_migrated``.
    """
    from tests.cli.test_operations import _migrated

    url = _migrated(tmp_path / "retag.db")
    assert main(["--database-url", url, "seed"]) == 0

    engine = create_db_engine(url)
    try:
        with build_session_factory(engine)() as session:
            source = session.scalars(select(Source).where(Source.slug == "bbc-news")).one()
            session.add(
                FeedItem(
                    source_id=source.id,
                    canonical_url=SPORT_URL,
                    title="Highlights",
                    published_at=NOW,
                    fetched_at=NOW,
                )
            )
            session.commit()
    finally:
        engine.dispose()
    return url


def rule_links(url: str) -> int:
    engine = create_db_engine(url)
    try:
        with build_session_factory(engine)() as session:
            return len(
                session.scalars(
                    select(FeedItemTopic.topic_id).where(FeedItemTopic.origin == TopicOrigin.RULE)
                ).all()
            )
    finally:
        engine.dispose()


def test_the_command_applies_and_reports(
    seeded_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert main(["--database-url", seeded_db, "retag"]) == 0

    out = capsys.readouterr().out
    assert "examined 1 item" in out
    assert "added 1 topic link" in out
    assert rule_links(seeded_db) == 1


def test_the_command_is_idempotent(seeded_db: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--database-url", seeded_db, "retag"]) == 0
    capsys.readouterr()

    assert main(["--database-url", seeded_db, "retag"]) == 0
    assert "every rule link is already correct" in capsys.readouterr().out


def test_the_command_takes_dry_run(seeded_db: str, capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    assert main(["--database-url", seeded_db, "retag", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "would add 1 topic link" in out
    assert rule_links(seeded_db) == 0
