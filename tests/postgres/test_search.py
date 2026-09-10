"""Feed search on PostgreSQL: the tsquery branch, and the index behind it.

Two things here that the SQLite suite structurally cannot cover.

The first is the *matching*, which is genuinely different. SQLite runs a
substring ``LIKE``; PostgreSQL runs ``plainto_tsquery`` against a
``to_tsvector``, so it stems, and a developer reading a search result on
a laptop is reading a different function's output from the one production
runs.

The second is the *index*, and it is the reason this file exists at all.
``ix_feed_items_search`` is an expression index, and PostgreSQL will only
use one when the indexed expression matches the query's character for
character. A divergence between
``app.services.feed.SEARCH_DOCUMENT_SQL`` and the ``DOCUMENT`` constant
in revision ``b7c1e0a94f6d`` produces no error at any point: the index
builds, ``CREATE INDEX`` reports success, queries return correct rows,
and every one of them is a sequential scan with a ``to_tsvector`` call
per row. That is exactly the shape of guard this repository has shipped
six times under other names — one that cannot fail — so the assertion
here is on the *plan*, which is the only artefact that changes.

``enable_seqscan = off`` is deliberate and is not cheating. The question
is not "does the planner prefer this index on a small table", which is a
fact about statistics and would make the test flap with the row count.
It is "can this index serve this query at all", and a planner told not to
scan sequentially answers exactly that: with a matching expression it
uses the index, and with a mismatched one it has nothing to use and falls
back to the sequential scan it was told to avoid.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.db.models import Base, FeedItem, MuteKind, Source, User, UserMutedTerm
from app.services.feed import SEARCH_DOCUMENT_SQL, mute_predicates, search_predicate
from tests.postgres.conftest import postgres_url
from tests.postgres.conftest import pytestmark as _pytestmark

pytestmark = _pytestmark

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
INDEX_NAME = "ix_feed_items_search"
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)

#: Enough rows that the plan is about the index rather than about a table
#: PostgreSQL would read in one page whatever it was asked.
FILLER_ROWS = 400


@pytest.fixture
def migrated(pg_engine: Engine) -> Iterator[sessionmaker[Session]]:
    """A database built by Alembic, not by ``create_all``.

    The index under test is created by a migration and is deliberately not
    declared on the model — declaring it there would emit it on SQLite
    too, where nothing can use it. So ``create_all`` does not produce it,
    and a fixture that used it would be testing a schema production never
    has.
    """
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", postgres_url())

    Base.metadata.drop_all(pg_engine)
    with pg_engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    command.upgrade(config, "head")
    try:
        yield sessionmaker(pg_engine, expire_on_commit=False)
    finally:
        command.downgrade(config, "base")
        with pg_engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        Base.metadata.create_all(pg_engine)


@pytest.fixture
def corpus(migrated: sessionmaker[Session]) -> sessionmaker[Session]:
    with migrated() as session:
        source = Source(
            slug="searchable",
            name="Searchable",
            feed_url="https://searchable.example/rss",
            website_url="https://searchable.example",
        )
        session.add(source)
        session.flush()

        session.add_all(
            [
                FeedItem(
                    source_id=source.id,
                    canonical_url="https://searchable.example/bookmark",
                    title="How the bookmark survives retention",
                    summary="An explicit keep, exempt from the prune.",
                    published_at=NOW,
                ),
                FeedItem(
                    source_id=source.id,
                    canonical_url="https://searchable.example/catalogue",
                    title="Seeding the catalogue",
                    summary="Sources and topics, one command.",
                    published_at=NOW - timedelta(minutes=1),
                ),
            ]
        )
        session.add_all(
            FeedItem(
                source_id=source.id,
                canonical_url=f"https://searchable.example/filler-{index}",
                title=f"Filler item {index}",
                summary="Nothing to find here.",
                published_at=NOW - timedelta(minutes=index + 2),
            )
            for index in range(FILLER_ROWS)
        )
        session.commit()
    with pg_engine_of(migrated).begin() as connection:
        connection.execute(text("ANALYZE feed_items"))
    return migrated


def pg_engine_of(factory: sessionmaker[Session]) -> Engine:
    bind = factory.kw["bind"]
    assert isinstance(bind, Engine)
    return bind


def _titles(session: Session, query: str) -> list[str]:
    predicate = search_predicate(session, query)
    assert predicate is not None
    return list(session.scalars(select(FeedItem.title).where(predicate)))


def test_search_stems_on_postgres(corpus: sessionmaker[Session]) -> None:
    """``bookmarks`` finds "bookmark". This is the behaviour SQLite's
    substring match does not have, and the reason the two halves of
    ``search_predicate`` are documented as differing in recall."""
    with corpus() as session:
        assert _titles(session, "bookmarks") == ["How the bookmark survives retention"]


def test_search_does_not_match_a_bare_substring_on_postgres(
    corpus: sessionmaker[Session],
) -> None:
    """The other side of the same coin: ``cat`` is not a prefix search, so
    it does not find the catalogue. SQLite would."""
    with corpus() as session:
        assert _titles(session, "cat") == []


def test_search_matches_the_summary_as_well_as_the_title_on_postgres(
    corpus: sessionmaker[Session],
) -> None:
    """The indexed expression concatenates both columns; a query reaching
    only the title would still pass every test above."""
    with corpus() as session:
        assert _titles(session, "prune") == ["How the bookmark survives retention"]


def test_multiple_terms_are_all_required_on_postgres(corpus: sessionmaker[Session]) -> None:
    """``plainto_tsquery`` ANDs, which is what the SQLite branch is written
    to agree with."""
    with corpus() as session:
        assert _titles(session, "bookmark retention") == ["How the bookmark survives retention"]
        assert _titles(session, "bookmark catalogue") == []


def test_the_search_index_can_serve_the_search_predicate(
    corpus: sessionmaker[Session],
) -> None:
    """The guard this file exists for. See the module docstring for why the
    assertion is on the plan and why sequential scans are disabled."""
    with corpus() as session:
        predicate = search_predicate(session, "bookmarks")
        assert predicate is not None
        statement = select(FeedItem.id).where(predicate)
        compiled = statement.compile(session.get_bind(), compile_kwargs={"literal_binds": True})

        session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(session.scalars(text(f"EXPLAIN {compiled}")))

    assert INDEX_NAME in plan, plan
    assert "Seq Scan" not in plan, plan


def test_the_migration_and_the_service_spell_the_document_identically() -> None:
    """A cheap string comparison in front of the expensive plan assertion
    above, so a divergence names itself instead of surfacing as a
    sequential scan somebody has to read a plan to notice.

    It is not a substitute for that test: two identical strings can still
    fail to match an index if either is wrapped differently at the point
    of use, which is a thing only the planner knows.
    """
    revision = (
        REPO_ROOT / "alembic" / "versions" / "b7c1e0a94f6d_feed_item_search_index.py"
    ).read_text()

    assert f'DOCUMENT = "{SEARCH_DOCUMENT_SQL}"' in revision


# --- muting, which is the same match negated -----------------------------


@pytest.fixture
def mute_corpus(migrated: sessionmaker[Session]) -> sessionmaker[Session]:
    with migrated() as session:
        user = User(github_id=101405, github_login="darkflib")
        source = Source(
            slug="mutable",
            name="Mutable",
            feed_url="https://mutable.example/rss",
            website_url="https://mutable.example",
        )
        session.add_all([user, source])
        session.flush()
        session.add_all(
            FeedItem(
                source_id=source.id,
                canonical_url=f"https://mutable.example/{key}",
                title=title,
                summary=summary,
                published_at=NOW - timedelta(minutes=index),
            )
            for index, (key, title, summary) in enumerate(
                [
                    ("league", "Premier League clubs agree a new deal", None),
                    ("premier", "A premier example of cache invalidation", None),
                    ("keyset", "Keyset pagination in Postgres", "OFFSET degrades with depth."),
                    ("stop", "The the the", "Nothing but stop words up there."),
                ]
            )
        )
        session.commit()
    return migrated


def _muted_titles(session: Session, *terms: str) -> list[str]:
    """Titles surviving *terms* as muted words."""
    user = session.scalars(select(User)).one()
    session.add_all(UserMutedTerm(user_id=user.id, kind=MuteKind.WORD, term=term) for term in terms)
    session.flush()
    statement = select(FeedItem.title)
    for predicate in mute_predicates(session, user):
        statement = statement.where(predicate)
    return sorted(session.scalars(statement))


def test_a_muted_phrase_requires_all_its_words_on_postgres(
    mute_corpus: sessionmaker[Session],
) -> None:
    """``||`` between per-term ``plainto_tsquery`` calls, not one query over
    the joined string: "premier league" hides the league and leaves the
    article about caches, which a reader who dislikes football still wants."""
    with mute_corpus() as session:
        assert _muted_titles(session, "premier league") == [
            "A premier example of cache invalidation",
            "Keyset pagination in Postgres",
            "The the the",
        ]


def test_a_muted_word_stems_on_postgres(mute_corpus: sessionmaker[Session]) -> None:
    """Muting "club" takes "clubs". The SQLite branch reaches the same item
    by substring and would also take "clubhouse"; this is the divergence
    ``_text_match`` documents, from the side where it costs most."""
    with mute_corpus() as session:
        assert "Premier League clubs agree a new deal" not in _muted_titles(session, "club")


def test_several_muted_words_are_one_match_on_postgres(
    mute_corpus: sessionmaker[Session],
) -> None:
    """Any-of, and in a single ``@@`` against a single vector — the reason
    the terms are OR-ed into one tsquery rather than given a predicate
    each."""
    with mute_corpus() as session:
        assert _muted_titles(session, "league", "keyset") == [
            "A premier example of cache invalidation",
            "The the the",
        ]


def test_a_mute_of_only_stop_words_mutes_nothing(
    mute_corpus: sessionmaker[Session],
) -> None:
    """``plainto_tsquery('english', 'the')`` is the empty tsquery, and
    ``vector @@ ''`` is false — so the negation is true and nothing is
    hidden. Worth pinning because the failure would be silent and total:
    if an empty query matched instead, muting a stop word would empty the
    reader's feed."""
    with mute_corpus() as session:
        assert len(_muted_titles(session, "the")) == 4
