"""``sre-tab detect-languages`` — the pass for items stored before detection.

Ingest detects once, on arrival, and never rewrites a row. Every item
already in the retained window when detection shipped therefore carries
``NULL``, which the feed never hides — so until this runs, a reader's
language choice has nothing to act on.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime

import pytest
from fast_langdetect import FastLangdetectError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cli import main
from app.cli import operations as ops
from app.db.engine import create_db_engine
from app.db.models import FeedItem, Source
from app.db.session import build_session_factory
from app.ingest import language

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)

#: Title to the language the detector gives it.
TITLES: dict[str, str | None] = {
    "Como uma gestão despreparada pode custar a sua carreira": "pt",
    "Why your Kubernetes pods keep getting OOMKilled": "en",
    "Rust vs Go": None,
}


def _seed(session: Session) -> None:
    source = Source(
        slug="dev-to", name="DEV", feed_url="https://dev.to/feed", website_url="https://dev.to/"
    )
    session.add(source)
    session.flush()
    session.add_all(
        FeedItem(
            source_id=source.id,
            canonical_url=f"https://dev.to/someone/{index}",
            title=title,
            published_at=NOW,
            fetched_at=NOW,
        )
        for index, title in enumerate(TITLES)
    )
    session.commit()


def _languages(session: Session) -> dict[str, str | None]:
    return dict(session.execute(select(FeedItem.title, FeedItem.language)).tuples().all())


def test_it_labels_what_ingest_never_saw(db_session: Session) -> None:
    _seed(db_session)

    report = ops.detect_item_languages(db_session)
    db_session.commit()

    assert _languages(db_session) == TITLES
    # The short title was NULL and stays NULL, so it is not a change.
    assert (report.items_examined, report.items_changed) == (3, 2)


def test_a_dry_run_writes_nothing(db_session: Session) -> None:
    _seed(db_session)

    report = ops.detect_item_languages(db_session, dry_run=True)
    db_session.commit()

    assert report.items_changed == 2
    assert set(_languages(db_session).values()) == {None}


def test_a_stale_answer_is_taken_back(db_session: Session) -> None:
    """The whole window, not only ``NULL`` rows: an answer a stricter
    detector would no longer give has to be removable, or raising the
    threshold would only ever apply to new items."""
    _seed(db_session)
    short = db_session.scalars(select(FeedItem).where(FeedItem.title == "Rust vs Go")).one()
    short.language = "sl"
    db_session.commit()

    ops.detect_item_languages(db_session)
    db_session.commit()

    assert _languages(db_session)["Rust vs Go"] is None


def test_the_pass_crosses_batches(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keyset batches by id; one row per batch makes an off-by-one at a
    batch boundary skip or repeat an item rather than go unnoticed."""
    monkeypatch.setattr(ops, "RETAG_BATCH", 1)
    _seed(db_session)

    report = ops.detect_item_languages(db_session)
    db_session.commit()

    assert report.items_examined == 3
    assert _languages(db_session) == TITLES


def test_a_detector_failure_leaves_the_stored_language_alone(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``detect_language`` answers a failure as ``None`` so ingest carries
    on; read that way here, a model that would not load would erase every
    stored answer."""
    _seed(db_session)
    ops.detect_item_languages(db_session)
    db_session.commit()

    def _broken(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        raise FastLangdetectError("model exploded")

    monkeypatch.setattr(language._DETECTOR, "detect", _broken)
    report = ops.detect_item_languages(db_session)
    db_session.commit()

    assert (report.items_changed, report.items_failed) == (0, 3)
    assert _languages(db_session) == TITLES


# --- the command ---------------------------------------------------------


@pytest.fixture
def seeded_db(tmp_path: pathlib.Path) -> str:
    """A migrated file database, so the CLI's own session and commit are
    what write — the reasoning ``tests/cli/test_operations.py`` gives."""
    from tests.cli.test_operations import _migrated

    url = _migrated(tmp_path / "languages.db")
    engine = create_db_engine(url)
    try:
        with build_session_factory(engine)() as session:
            _seed(session)
    finally:
        engine.dispose()
    return url


def _stored(url: str) -> dict[str, str | None]:
    engine = create_db_engine(url)
    try:
        with build_session_factory(engine)() as session:
            return _languages(session)
    finally:
        engine.dispose()


def test_the_command_applies_and_reports(
    seeded_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--database-url", seeded_db, "detect-languages"]) == 0

    out = capsys.readouterr().out
    assert "examined 3 items" in out
    assert "changed the language of 2 items" in out
    assert _stored(seeded_db) == TITLES


def test_the_command_is_idempotent(seeded_db: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--database-url", seeded_db, "detect-languages"]) == 0
    capsys.readouterr()

    assert main(["--database-url", seeded_db, "detect-languages"]) == 0
    assert "already current" in capsys.readouterr().out


def test_the_command_fails_when_detection_does(
    seeded_db: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _broken(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        raise FastLangdetectError("model exploded")

    monkeypatch.setattr(language._DETECTOR, "detect", _broken)

    assert main(["--database-url", seeded_db, "detect-languages"]) == 1
    assert "detection failed for 3 items" in capsys.readouterr().out


def test_the_command_takes_dry_run(seeded_db: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--database-url", seeded_db, "detect-languages", "--dry-run"]) == 0

    assert "would change the language of 2 items" in capsys.readouterr().out
    assert set(_stored(seeded_db).values()) == {None}
