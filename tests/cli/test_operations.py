"""Operator CLI: seeding, catalogue management, and the status view."""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta

import pytest
from alembic.config import Config
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.cli import main
from app.cli import operations as ops
from app.cli.catalogue import InvalidMediumTag
from app.db.engine import create_db_engine
from app.db.models import Source, SourceStatus, SourceTopic, Topic
from app.db.session import build_session_factory
from app.ingest.status import SourceStatusRegistry
from app.services.preferences import DEFAULT_SOURCE_SLUGS
from tests.test_migrations import REPO_ROOT

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def _migrated(path: pathlib.Path) -> str:
    """A file database at head, so the CLI's own session handling is
    exercised rather than a shared in-memory engine."""
    url = f"sqlite:///{path}"
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    return url


@pytest.fixture
def seeded(db_session: Session) -> Session:
    ops.seed_catalogue(db_session)
    db_session.commit()
    return db_session


# --- seeding ------------------------------------------------------------


def test_seed_installs_the_catalogue_and_the_taxonomy(db_session: Session) -> None:
    report = ops.seed_catalogue(db_session)
    db_session.commit()

    assert report.changed is True
    assert len(db_session.scalars(select(Topic)).all()) == 11
    assert len(db_session.scalars(select(Source)).all()) == 7
    assert len(db_session.scalars(select(SourceTopic)).all()) == 11


def test_seeding_twice_changes_nothing(seeded: Session) -> None:
    report = ops.seed_catalogue(seeded)
    seeded.commit()

    assert report.changed is False
    assert len(seeded.scalars(select(Source)).all()) == 7
    assert len(seeded.scalars(select(SourceTopic)).all()) == 11


def test_reseeding_does_not_undo_an_operator_decision(seeded: Session) -> None:
    """Disabling a source is a decision; re-running the seed is not the
    place to reverse it."""
    ops.set_source_enabled(seeded, "bbc-news", enabled=False)
    seeded.commit()

    ops.seed_catalogue(seeded)
    seeded.commit()

    source = seeded.scalar(select(Source).where(Source.slug == "bbc-news"))
    assert source is not None
    assert source.enabled is False


def test_the_default_selection_resolves_against_the_seed(seeded: Session) -> None:
    """S1's load-bearing property, asserted against the database rather
    than against the constant."""
    found = seeded.scalars(select(Source.slug).where(Source.slug.in_(DEFAULT_SOURCE_SLUGS))).all()
    assert set(found) == set(DEFAULT_SOURCE_SLUGS)


def test_every_seeded_source_carries_its_topics(seeded: Session) -> None:
    rows = seeded.execute(
        select(Source.slug, Topic.slug)
        .join(SourceTopic, SourceTopic.source_id == Source.id)
        .join(Topic, Topic.id == SourceTopic.topic_id)
    ).all()
    by_source: dict[str, set[str]] = {}
    for source_slug, topic_slug in rows:
        by_source.setdefault(source_slug, set()).add(topic_slug)

    assert by_source["hacker-news"] == {"tech-industry"}
    assert by_source["lobsters"] == {"open-source", "tech-industry"}
    assert by_source["lwn"] == {"open-source", "security"}
    assert by_source["bbc-news"] == {"uk-news", "world-news"}


# --- adding sources -----------------------------------------------------


def test_add_source_stores_the_normalised_url(seeded: Session) -> None:
    source = ops.add_source(
        seeded,
        slug="phoronix",
        name="Phoronix",
        feed_url="https://www.phoronix.com/rss.php",
        website_url="https://www.phoronix.com/",
        refresh_minutes=60,
        topics=["hardware"],
    )
    seeded.commit()
    assert source.feed_url == "https://www.phoronix.com/rss.php"
    assert seeded.scalars(
        select(SourceTopic.topic_id).where(SourceTopic.source_id == source.id)
    ).all()


@pytest.mark.parametrize(
    "feed_url",
    [
        "http://example.com/rss",  # not https
        "https://user:pass@example.com/rss",  # credentials
        "https://example.com:8080/rss",  # non-standard port
        "https://127.0.0.1/rss",  # loopback literal
        "https://2130706433/rss",  # obfuscated loopback
        "https://[::1]/rss",  # IPv6 loopback
        "https://169.254.169.254/latest",  # link-local metadata
        "https://localhost/rss",  # single-label host
        "https://intranet.local/rss",  # non-public suffix
        "https://api.example.com/graphql",  # v2 deferral
        "https://example.com/sitemap.xml",  # v2 deferral
        # A trailing dot is enough to get an obfuscated literal past
        # httpx's parser; the host only *becomes* one after the guard
        # normalises it away. The fetcher refuses all of these, so
        # configuration time must too — that is what this function is
        # for, and accepting them here means a source that fails hours
        # later with a reason no one is reading.
        "https://0x7f.0.0.1./rss",  # loopback, after normalisation
        "https://127.1./rss",  # loopback short form
        "https://0177.1./rss",  # loopback, octal short form
        "https://0.0.0.0./rss",  # unspecified
        "https://0177.0.0.1./rss",  # octal dotted-quad
    ],
)
def test_add_source_refuses_a_url_the_fetcher_would_refuse(seeded: Session, feed_url: str) -> None:
    """The guard runs at configuration time, with no DNS, so an operator
    reads the reason now instead of finding a failing source later."""
    with pytest.raises(ops.OperatorError):
        ops.add_source(
            seeded,
            slug="hostile",
            name="Hostile",
            feed_url=feed_url,
            website_url="https://example.com/",
            refresh_minutes=30,
        )


def test_configuration_time_validation_is_a_fixpoint() -> None:
    """Validating the stored URL again must never change or refuse it.

    The property this rests on: what ``add`` stores is what ``fetch``
    later validates, so any URL whose *normalised* form would be refused
    has to be refused now rather than accepted and stored.
    """
    for feed_url in (
        "https://www.phoronix.com/rss.php",
        "https://feeds.bbci.co.uk/news/rss.xml",
        "https://EXAMPLE.com/Feed?x=1",
        "https://example.com./rss",
    ):
        stored = ops.validate_feed_url(feed_url)
        assert ops.validate_feed_url(stored) == stored


#: Each of these reaches a consumer that assumes something different about
#: a slug's shape. The comma and the plus are the client's own separators —
#: one splits the slug in two inside the browser's query string, the other
#: aliases two selections onto one cache key — and neither produces an
#: error anywhere, just a source that lists correctly and filters to
#: nothing. The over-length case is a dialect divergence: ``String(64)``
#: is enforced by PostgreSQL and ignored by SQLite, so it works in
#: development and fails in production.
BAD_SLUGS = [
    "a,b",
    "a+b",
    "*",
    "Upper-Case",
    "with space",
    "trailing-",
    "-leading",
    "double--hyphen",
    "under_score",
    "",
    "x" * 65,
]


@pytest.mark.parametrize("slug", BAD_SLUGS)
def test_add_source_rejects_a_slug_the_client_cannot_round_trip(seeded: Session, slug: str) -> None:
    with pytest.raises(ops.OperatorError):
        ops.add_source(
            seeded,
            slug=slug,
            name="Whatever",
            feed_url="https://example.com/feed.xml",
            website_url="https://example.com/",
            refresh_minutes=60,
        )


@pytest.mark.parametrize("slug", BAD_SLUGS)
def test_add_topic_rejects_the_same_slugs(seeded: Session, slug: str) -> None:
    with pytest.raises(ops.OperatorError):
        ops.add_topic(seeded, slug=slug, name="Whatever")


def test_the_slug_check_runs_before_the_uniqueness_check(seeded: Session) -> None:
    # Order matters for the message the operator reads: told "already
    # exists" about a slug that could never have been added, they would go
    # looking for a row that is not there.
    with pytest.raises(ops.OperatorError, match="not usable"):
        ops.add_topic(seeded, slug="a,b", name="Comma")


def test_nonconforming_slugs_reports_rows_that_predate_the_check(seeded: Session) -> None:
    # Enforcement at add time binds only what is added after it, so the
    # existing catalogue is reported rather than migrated — a slug cannot
    # be rewritten in place without breaking every saved selection naming
    # it. Written straight to the table, which is how such a row got there.
    seeded.add(Topic(slug="legacy,topic", name="Legacy"))
    seeded.flush()

    found = ops.nonconforming_slugs(seeded)
    assert [(kind, slug) for kind, slug, _ in found] == [("topic", "legacy,topic")]


def test_nonconforming_slugs_is_empty_for_the_seeded_catalogue(seeded: Session) -> None:
    assert ops.nonconforming_slugs(seeded) == []


def test_add_source_rejects_a_duplicate_slug(seeded: Session) -> None:
    with pytest.raises(ops.OperatorError):
        ops.add_source(
            seeded,
            slug="lobsters",
            name="Lobsters again",
            feed_url="https://lobste.rs/rss.json",
            website_url="https://lobste.rs/",
            refresh_minutes=30,
        )


def test_add_source_rejects_an_unknown_topic(seeded: Session) -> None:
    with pytest.raises(ops.OperatorError):
        ops.add_source(
            seeded,
            slug="whatever",
            name="Whatever",
            feed_url="https://example.com/feed.xml",
            website_url="https://example.com/",
            refresh_minutes=30,
            topics=["not-a-topic"],
        )


def test_add_medium_tag_creates_an_ordinary_source(seeded: Session) -> None:
    source = ops.add_medium_tag(seeded, "python", topics=["python"])
    seeded.commit()

    assert source.slug == "medium-python"
    assert source.feed_url == "https://medium.com/feed/tag/python"
    assert source.refresh_minutes == 60
    stored = seeded.scalar(select(Source).where(Source.slug == "medium-python"))
    assert stored is not None


def test_add_medium_tag_refuses_a_path_traversal(seeded: Session) -> None:
    with pytest.raises(InvalidMediumTag):
        ops.add_medium_tag(seeded, "../../admin")


# --- enable, disable, topics -------------------------------------------


def test_enable_and_disable_round_trip(seeded: Session) -> None:
    assert ops.set_source_enabled(seeded, "lwn", enabled=False).enabled is False
    assert ops.set_source_enabled(seeded, "lwn", enabled=True).enabled is True
    assert ops.set_topic_enabled(seeded, "hardware", enabled=False).enabled is False
    assert ops.set_topic_enabled(seeded, "hardware", enabled=True).enabled is True


def test_operating_on_an_unknown_slug_is_an_operator_error(seeded: Session) -> None:
    with pytest.raises(ops.OperatorError):
        ops.set_source_enabled(seeded, "nope", enabled=False)
    with pytest.raises(ops.OperatorError):
        ops.set_topic_enabled(seeded, "nope", enabled=False)


def test_set_source_topics_replaces_the_selection(seeded: Session) -> None:
    source = ops.set_source_topics(seeded, "lobsters", ["python", "devops"])
    seeded.commit()

    slugs = set(
        seeded.scalars(
            select(Topic.slug)
            .join(SourceTopic, SourceTopic.topic_id == Topic.id)
            .where(SourceTopic.source_id == source.id)
        )
    )
    assert slugs == {"python", "devops"}


def test_add_topic_extends_the_taxonomy(seeded: Session) -> None:
    ops.add_topic(seeded, slug="rust", name="Rust")
    seeded.commit()
    assert seeded.scalar(select(Topic).where(Topic.slug == "rust")) is not None
    with pytest.raises(ops.OperatorError):
        ops.add_topic(seeded, slug="rust", name="Rust again")


# --- status view --------------------------------------------------------


def test_status_lists_every_source_including_never_fetched(seeded: Session) -> None:
    views = ops.refresh_status(seeded)
    assert [view.slug for view in views] == sorted(view.slug for view in views)
    assert len(views) == 7
    assert all(view.state == "never fetched" for view in views)


def test_status_reads_what_the_scheduler_wrote(seeded: Session, engine: Engine) -> None:
    """The point of the table: a different process can see the outcome."""
    lobsters = seeded.scalar(select(Source).where(Source.slug == "lobsters"))
    assert lobsters is not None

    registry = SourceStatusRegistry(build_session_factory(engine))
    registry.record_failure(
        source_id=lobsters.id,
        slug="lobsters",
        refresh_minutes=30,
        error_class="FetchTimeoutError",
        detail="timed out",
        now=NOW,
    )

    seeded.expire_all()
    view = next(view for view in ops.refresh_status(seeded) if view.slug == "lobsters")
    assert view.state == "failing (1)"
    assert view.last_error_class == "FetchTimeoutError"
    assert view.consecutive_failures == 1


def test_status_reports_a_disabled_source_as_disabled(seeded: Session) -> None:
    ops.set_source_enabled(seeded, "bbc-news", enabled=False)
    seeded.commit()
    view = next(view for view in ops.refresh_status(seeded) if view.slug == "bbc-news")
    assert view.state == "disabled"


def test_status_survives_a_source_with_no_status_row(seeded: Session) -> None:
    assert seeded.scalars(select(SourceStatus)).all() == []
    assert len(ops.refresh_status(seeded)) == 7


# --- the argparse front end ---------------------------------------------


def test_the_cli_seeds_and_lists(
    tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end through ``main``, session handling included."""
    url = _migrated(tmp_path_factory.mktemp("cli") / "cli.db")

    assert main(["--database-url", url, "seed"]) == 0
    assert main(["--database-url", url, "sources", "list"]) == 0
    assert main(["--database-url", url, "topics", "list"]) == 0
    # No enabled source is failing, so status is a clean exit.
    assert main(["--database-url", url, "status"]) == 0

    output = capsys.readouterr().out
    assert "hacker-news" in output
    assert "never fetched" in output


def test_the_cli_reports_an_operator_error_without_a_traceback(
    tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    url = _migrated(tmp_path_factory.mktemp("cli") / "cli.db")
    code = main(
        [
            "--database-url",
            url,
            "sources",
            "add",
            "--slug",
            "metadata",
            "--name",
            "Metadata",
            "--feed-url",
            "https://169.254.169.254/latest/meta-data/",
            "--website-url",
            "https://example.com/",
        ]
    )
    assert code == 2
    assert "refused" in capsys.readouterr().err


def test_the_cli_status_exits_non_zero_when_a_source_is_failing(
    tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    """So a monitoring job can call it and mean it."""
    url = _migrated(tmp_path_factory.mktemp("cli") / "cli.db")
    assert main(["--database-url", url, "seed"]) == 0

    engine = create_db_engine(url)
    factory = build_session_factory(engine)
    with factory() as session:
        source_id = session.scalar(select(Source.id).where(Source.slug == "lwn"))
    assert source_id is not None
    SourceStatusRegistry(factory).record_failure(
        source_id=source_id,
        slug="lwn",
        refresh_minutes=60,
        error_class="UpstreamStatusError",
        detail="503 from upstream",
        now=NOW - timedelta(minutes=5),
    )
    engine.dispose()

    assert main(["--database-url", url, "status"]) == 1
    assert "UpstreamStatusError" in capsys.readouterr().out


def test_the_cli_status_exits_non_zero_for_a_slug_that_predates_the_check(
    tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed slug is not a refresh failure — the source fetches
    perfectly and simply cannot be filtered to — so it is reported as its
    own thing. It still fails the command, because exiting non-zero is how
    the monitoring job finds out."""
    url = _migrated(tmp_path_factory.mktemp("cli") / "cli.db")
    assert main(["--database-url", url, "seed"]) == 0

    engine = create_db_engine(url)
    factory = build_session_factory(engine)
    with factory() as session:
        session.add(Topic(slug="legacy,topic", name="Legacy"))
        session.commit()
    engine.dispose()

    assert main(["--database-url", url, "status"]) == 1
    output = capsys.readouterr().out
    assert "legacy,topic" in output
    # Reported as a slug problem, not misfiled under refresh failures.
    assert "predate the format check" in output
