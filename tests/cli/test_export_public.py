"""``sre-tab export-public``: the public snapshot and darkflib.com's kev.json.

The builders are tested against the shared in-memory engine; the command
is driven through ``main`` against a migrated file database, like the
rest of the CLI suite, so its own session handling is under test too.
"""

from __future__ import annotations

import json
import os
import pathlib
import stat
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.cli import main
from app.db.engine import create_db_engine
from app.db.models import Bookmark, FeedItem, Source, SourceStatus, User, UserReadItem
from app.db.session import build_session_factory
from app.ingest.kev import CATALOGUE_PAGE_URL, CISA_KEV_FEED_URL, parse_kev_catalogue
from app.ingest.normalise import MAX_SUMMARY_LENGTH, normalise_entry
from app.services import public_snapshot as snapshots
from tests.cli.test_operations import _migrated
from tests.ingest.test_kev import catalogue, vulnerability

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
KEV_PAGE = f"{CATALOGUE_PAGE_URL}?search_api_fulltext="


# --- fixtures and helpers ---------------------------------------------------


def _source(
    db: Session,
    slug: str,
    *,
    feed_url: str | None = None,
    website_url: str | None = None,
    last_success: datetime | None = NOW - timedelta(minutes=20),
    enabled: bool = True,
) -> Source:
    source = Source(
        slug=slug,
        name=f"{slug} name",
        feed_url=feed_url or f"https://{slug}.example.org/feed.xml",
        website_url=website_url or f"https://www.{slug}.example.org/",
        enabled=enabled,
    )
    db.add(source)
    db.flush()
    if last_success is not None:
        db.add(
            SourceStatus(
                source_id=source.id, last_fetched_at=last_success, last_success_at=last_success
            )
        )
    db.flush()
    return source


def _item(
    db: Session,
    source: Source,
    url: str,
    *,
    title: str = "A headline",
    summary: str | None = "What happened.",
    published: datetime = NOW,
) -> FeedItem:
    item = FeedItem(
        source_id=source.id,
        canonical_url=url,
        title=title,
        summary=summary,
        published_at=published,
    )
    db.add(item)
    db.flush()
    return item


def _kev_source(db: Session, **kwargs: Any) -> Source:
    return _source(
        db,
        "cisa-kev",
        feed_url=CISA_KEV_FEED_URL,
        website_url="https://www.cisa.gov/known-exploited-vulnerabilities-catalog",
        **kwargs,
    )


def _loaded(db: Session, source: Source) -> Source:
    """*source* as the command loads it, with its status eagerly attached."""
    (loaded,) = snapshots.load_sources(db, [source.slug])
    return loaded


def _ingest_kev(db: Session, source: Source, *entries: dict[str, Any]) -> list[FeedItem]:
    """KEV rows exactly as ingest writes them: the real parse, then the real normaliser."""
    rows: list[FeedItem] = []
    for entry in parse_kev_catalogue(catalogue(*entries)).entries:
        normalised = normalise_entry(entry, fetched_at=NOW)
        assert normalised is not None
        rows.append(
            _item(
                db,
                source,
                normalised.canonical_url,
                title=normalised.title,
                summary=normalised.summary,
                published=normalised.published_at,
            )
        )
    return rows


# --- parsing the KEV rows back ------------------------------------------------


def test_kev_fields_survive_the_round_trip_through_ingest(db_session: Session) -> None:
    source = _kev_source(db_session)
    (known, plain, bare) = _ingest_kev(
        db_session,
        source,
        vulnerability("CVE-2026-1001", "2026-10-23", knownRansomwareCampaignUse="Known"),
        vulnerability("CVE-2026-1002", "2026-10-22", dueDate=None),
        vulnerability("CVE-2026-1003", "2026-10-21", vulnerabilityName=""),
    )

    fields = snapshots.parse_kev_item(known.title, known.summary)
    assert fields == snapshots.KevFields(
        cve="CVE-2026-1001",
        name="Example Widget Path Traversal Vulnerability",
        description="Example Widget contains a path traversal vulnerability.",
        due="2026-10-07",
        ransomware=True,
    )
    plain_fields = snapshots.parse_kev_item(plain.title, plain.summary)
    assert plain_fields is not None
    assert (plain_fields.due, plain_fields.ransomware) == (None, False)
    bare_fields = snapshots.parse_kev_item(bare.title, bare.summary)
    assert bare_fields is not None
    assert (bare_fields.cve, bare_fields.name) == ("CVE-2026-1003", "")


def test_kev_facts_survive_a_description_longer_than_the_summary_cap(db_session: Session) -> None:
    source = _kev_source(db_session)
    (row,) = _ingest_kev(
        db_session,
        source,
        vulnerability(
            "CVE-2026-1006",
            "2026-10-23",
            shortDescription="word " * (MAX_SUMMARY_LENGTH // 2),
            knownRansomwareCampaignUse="Known",
        ),
    )
    assert row.summary is not None
    assert len(row.summary) <= MAX_SUMMARY_LENGTH

    fields = snapshots.parse_kev_item(row.title, row.summary)
    assert fields is not None
    assert (fields.due, fields.ransomware) == ("2026-10-07", True)
    assert fields.description.endswith("…")


def test_a_summary_in_an_unexpected_shape_is_kept_whole() -> None:
    fields = snapshots.parse_kev_item(
        "CVE-2026-1004: Thing", "Remediate by 2026-11-01. Known to be used in ransomware campaigns!"
    )
    assert fields is not None
    assert (
        fields.description == "Remediate by 2026-11-01. Known to be used in ransomware campaigns!"
    )
    assert (fields.due, fields.ransomware) == (None, False)


def test_a_title_that_is_not_a_kev_title_is_refused() -> None:
    assert snapshots.parse_kev_item("Not a CVE: anything", None) is None
    assert snapshots.parse_kev_item("CVE-26-1: short id", None) is None


# --- text and links -----------------------------------------------------------


def test_clean_text_drops_controls_and_bidi_overrides_and_caps() -> None:
    assert snapshots.clean_text("  a\u202eb\x00c \n\t d  ", 50) == "abc d"
    assert snapshots.clean_text("x" * 20, 10) == "x" * 9 + "…"


@pytest.mark.parametrize(
    ("url", "kept"),
    [
        ("https://www.ncsc.gov.uk/news/a", True),
        ("https://ncsc.gov.uk/news/a", True),
        ("http://www.ncsc.gov.uk/news/a", False),
        ("https://evil.example/news/a", False),
        ("https://www.ncsc.gov.uk.evil.example/a", False),
        ("https://user@www.ncsc.gov.uk/a", False),
        ("javascript:alert(1)", False),
    ],
)
def test_links_must_be_https_on_the_sources_own_host(url: str, kept: bool) -> None:
    hosts = frozenset({"www.ncsc.gov.uk"})
    assert (snapshots.https_on_hosts(url, hosts) == url) is kept


# --- the public snapshot ------------------------------------------------------


def test_only_the_named_sources_are_published(db_session: Session) -> None:
    ncsc = _source(db_session, "uk-ncsc")
    other = _source(db_session, "hacker-news")
    _item(db_session, ncsc, "https://uk-ncsc.example.org/news/1")
    _item(db_session, other, "https://hacker-news.example.org/item/1")

    sources = snapshots.load_sources(db_session, ["uk-ncsc"])
    document, _ = snapshots.build_public_snapshot(db_session, sources, per_source=20)

    assert list(document["sources"]) == ["uk-ncsc"]
    assert {item["source"] for item in document["items"]} == {"uk-ncsc"}


def test_an_empty_source_list_is_refused_before_any_query(db_session: Session) -> None:
    with pytest.raises(ValueError, match="at least one"):
        snapshots.load_sources(db_session, [])


def test_items_carry_public_fields_and_nothing_a_reader_did(db_session: Session) -> None:
    ncsc = _source(db_session, "uk-ncsc")
    row = _item(db_session, ncsc, "https://uk-ncsc.example.org/news/1")
    user = User(github_id=1000001, github_login="octocat")
    db_session.add(user)
    db_session.flush()
    db_session.add_all(
        [
            Bookmark(user_id=user.id, feed_item_id=row.id),
            UserReadItem(user_id=user.id, feed_item_id=row.id),
        ]
    )
    db_session.flush()

    document, _ = snapshots.build_public_snapshot(
        db_session, snapshots.load_sources(db_session, ["uk-ncsc"]), per_source=20
    )

    # The whole key set, so a field added later has to be added here on purpose.
    assert set(document) == {"version", "sources", "items"}
    assert set(document["sources"]["uk-ncsc"]) == {"name", "website_url", "last_success_at"}
    (item,) = document["items"]
    assert set(item) == {"source", "title", "summary", "url", "published"}
    assert "octocat" not in json.dumps(document)


def test_items_are_newest_first_capped_per_source_and_links_checked(db_session: Session) -> None:
    ncsc = _source(db_session, "uk-ncsc")
    sans = _source(db_session, "sans-isc")
    for hours in range(5):
        _item(
            db_session,
            ncsc,
            f"https://uk-ncsc.example.org/news/{hours}",
            published=NOW - timedelta(hours=hours * 2),
        )
    _item(
        db_session, sans, "https://sans-isc.example.org/diary/1", published=NOW - timedelta(hours=1)
    )
    _item(
        db_session, sans, "http://sans-isc.example.org/diary/2", published=NOW - timedelta(hours=3)
    )
    _item(db_session, sans, "https://elsewhere.example/diary/3", published=NOW - timedelta(hours=5))

    document, stats = snapshots.build_public_snapshot(
        db_session, snapshots.load_sources(db_session, ["uk-ncsc", "sans-isc"]), per_source=3
    )

    published = [item["published"] for item in document["items"]]
    assert published == sorted(published, reverse=True)
    assert [item["url"] for item in document["items"]] == [
        "https://uk-ncsc.example.org/news/0",
        "https://sans-isc.example.org/diary/1",
        "https://uk-ncsc.example.org/news/1",
        "https://uk-ncsc.example.org/news/2",
    ]
    assert stats.kept == {"uk-ncsc": 3, "sans-isc": 1}
    assert stats.dropped == {"sans-isc": 2}


def test_a_source_that_never_refreshed_says_so(db_session: Session) -> None:
    ncsc = _source(db_session, "uk-ncsc", last_success=None)
    _item(db_session, ncsc, "https://uk-ncsc.example.org/news/1")
    document, _ = snapshots.build_public_snapshot(
        db_session, snapshots.load_sources(db_session, ["uk-ncsc"]), per_source=5
    )
    assert document["sources"]["uk-ncsc"]["last_success_at"] is None


def test_kev_items_gain_their_structured_fields(db_session: Session) -> None:
    kev = _kev_source(db_session)
    _ingest_kev(
        db_session,
        kev,
        vulnerability("CVE-2026-1001", "2026-10-23", knownRansomwareCampaignUse="Known"),
    )
    document, _ = snapshots.build_public_snapshot(
        db_session, snapshots.load_sources(db_session, ["cisa-kev"]), per_source=5
    )
    (item,) = document["items"]
    assert item["kev"] == {"cve": "CVE-2026-1001", "due": "2026-10-07", "ransomware": True}
    assert item["url"] == f"{KEV_PAGE}CVE-2026-1001"


def test_nothing_publishable_is_an_error_not_an_empty_document(db_session: Session) -> None:
    ncsc = _source(db_session, "uk-ncsc")
    _item(db_session, ncsc, "http://uk-ncsc.example.org/news/1")
    with pytest.raises(snapshots.ExportError):
        snapshots.build_public_snapshot(
            db_session, snapshots.load_sources(db_session, ["uk-ncsc"]), per_source=5
        )


# --- kev.json -------------------------------------------------------------------


def test_kev_json_is_byte_for_byte_the_format_darkflib_reads(db_session: Session) -> None:
    kev = _kev_source(
        db_session, last_success=datetime(2026, 10, 24, 11, 40, 5, 123456, tzinfo=UTC)
    )
    _ingest_kev(
        db_session,
        kev,
        vulnerability("CVE-2026-1001", "2026-10-23", knownRansomwareCampaignUse="Known"),
        vulnerability("CVE-2026-1002", "2026-10-22", dueDate=None, vulnerabilityName=""),
    )
    document, dropped = snapshots.build_kev_snapshot(db_session, _loaded(db_session, kev))

    # Field order and omission as kev-snapshot's Go struct tags give them:
    # due and url are omitempty, everything else always present.
    assert dropped == 0
    assert (
        snapshots.encode(document)
        == (
            '{"version":1,"fetched_at":"2026-10-24T11:40:05Z","entries":['
            '{"cve":"CVE-2026-1001","name":"Example Widget Path Traversal Vulnerability",'
            '"description":"Example Widget contains a path traversal vulnerability.",'
            '"added":"2026-10-23T00:00:00Z","due":"2026-10-07","ransomware":true,'
            f'"url":"{KEV_PAGE}CVE-2026-1001"}},'
            '{"cve":"CVE-2026-1002","name":"",'
            '"description":"Example Widget contains a path traversal vulnerability.",'
            '"added":"2026-10-22T00:00:00Z","ransomware":false,'
            f'"url":"{KEV_PAGE}CVE-2026-1002"}}'
            "]}\n"
        ).encode()
    )


def test_kev_json_ages_by_the_last_successful_refresh(db_session: Session) -> None:
    stalled = NOW - timedelta(hours=9)
    kev = _kev_source(db_session, last_success=stalled)
    _ingest_kev(db_session, kev, vulnerability("CVE-2026-1001", "2026-10-23"))
    document, _ = snapshots.build_kev_snapshot(db_session, _loaded(db_session, kev))
    assert document["fetched_at"] == snapshots.iso_timestamp(stalled)


def test_kev_json_drops_duplicates_and_off_host_links(db_session: Session) -> None:
    kev = _kev_source(db_session)
    _ingest_kev(db_session, kev, vulnerability("CVE-2026-1001", "2026-10-23"))
    _item(db_session, kev, "https://www.cisa.gov/other", title="CVE-2026-1001: Again")
    _item(db_session, kev, "https://cisa.gov/x", title="CVE-2026-1005: Bare host")
    _item(db_session, kev, "https://www.cisa.gov/y", title="Not a KEV entry")

    document, dropped = snapshots.build_kev_snapshot(db_session, _loaded(db_session, kev))

    assert dropped == 2
    by_cve = {entry["cve"]: entry for entry in document["entries"]}
    assert set(by_cve) == {"CVE-2026-1001", "CVE-2026-1005"}
    assert "url" not in by_cve["CVE-2026-1005"]


def test_kev_json_needs_a_successful_refresh(db_session: Session) -> None:
    kev = _kev_source(db_session, last_success=None)
    _ingest_kev(db_session, kev, vulnerability("CVE-2026-1001", "2026-10-23"))
    with pytest.raises(snapshots.ExportError, match="never refreshed"):
        snapshots.build_kev_snapshot(db_session, _loaded(db_session, kev))


# --- writing ------------------------------------------------------------------


def test_write_is_atomic_skips_identical_content_and_leaves_no_temporaries(
    tmp_path: pathlib.Path,
) -> None:
    assert snapshots.write_atomic(tmp_path, "public.json", b"one\n") is True
    target = tmp_path / "public.json"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    before = target.stat().st_mtime_ns

    assert snapshots.write_atomic(tmp_path, "public.json", b"one\n") is False
    assert target.stat().st_mtime_ns == before
    assert snapshots.write_atomic(tmp_path, "public.json", b"two\n") is True
    assert target.read_bytes() == b"two\n"
    assert [path.name for path in tmp_path.iterdir()] == ["public.json"]


def test_a_failed_write_keeps_the_previous_file(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshots.write_atomic(tmp_path, "public.json", b"good\n")

    def broken_replace(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError, match="disk full"):
        snapshots.write_atomic(tmp_path, "public.json", b"new\n")
    assert (tmp_path / "public.json").read_bytes() == b"good\n"
    assert [path.name for path in tmp_path.iterdir()] == ["public.json"]


@pytest.mark.parametrize("name", ["", "Public", "../x", "a/b", ".hidden", "a" * 64])
def test_snapshot_names_are_slug_shaped(name: str) -> None:
    with pytest.raises(ValueError):
        snapshots.snapshot_filename(name)


# --- the command ----------------------------------------------------------------


@pytest.fixture
def database(tmp_path_factory: pytest.TempPathFactory) -> str:
    url = _migrated(tmp_path_factory.mktemp("export") / "cli.db")
    engine: Engine = create_db_engine(url)
    try:
        with build_session_factory(engine)() as session:
            ncsc = _source(session, "uk-ncsc")
            _item(session, ncsc, "https://uk-ncsc.example.org/news/1")
            _source(session, "sans-isc")
            kev = _kev_source(session)
            _ingest_kev(session, kev, vulnerability("CVE-2026-1001", "2026-10-23"))
            _source(session, "dark", last_success=None)
            session.commit()
    finally:
        engine.dispose()
    return url


def _run(database: str, *args: str) -> int:
    return main(["--database-url", database, "export-public", *args])


def test_the_command_writes_both_files(
    database: str, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run(
        database,
        "--out",
        str(tmp_path),
        "--name",
        "security",
        "--sources",
        "uk-ncsc,sans-isc,cisa-kev",
        "--kev-json",
    )
    assert code == 0
    public = json.loads((tmp_path / "security.json").read_text())
    assert list(public["sources"]) == ["uk-ncsc", "sans-isc", "cisa-kev"]
    kev = json.loads((tmp_path / "kev.json").read_text())
    assert [entry["cve"] for entry in kev["entries"]] == ["CVE-2026-1001"]
    out = capsys.readouterr().out
    assert "security.json: written" in out
    assert "sans-isc: 0 items" in out

    assert (
        _run(
            database,
            "--out",
            str(tmp_path),
            "--name",
            "security",
            "--sources",
            "uk-ncsc,sans-isc,cisa-kev",
            "--kev-json",
        )
        == 0
    )
    assert "security.json: unchanged" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--sources", "uk-ncsc,nope"], "unknown source(s): nope"),
        (["--sources", " , "], "names no source"),
        (["--sources", "uk-ncsc", "--kev-json"], "needs the CISA KEV source"),
        (["--sources", "cisa-kev", "--kev-json", "--name", "kev"], "would overwrite"),
    ],
)
def test_operator_mistakes_exit_2_and_write_nothing(
    database: str,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    message: str,
) -> None:
    assert _run(database, "--out", str(tmp_path), *args) == 2
    assert message in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_a_missing_output_directory_is_an_operator_mistake(
    database: str, tmp_path: pathlib.Path
) -> None:
    assert _run(database, "--out", str(tmp_path / "absent"), "--sources", "uk-ncsc") == 2


def test_nothing_to_publish_exits_1_and_keeps_the_previous_files(
    database: str, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "public.json").write_bytes(b"previous\n")
    assert _run(database, "--out", str(tmp_path), "--sources", "sans-isc") == 1
    assert "previous snapshot is unchanged" in capsys.readouterr().err
    assert (tmp_path / "public.json").read_bytes() == b"previous\n"


def test_a_kev_failure_writes_neither_file(database: str, tmp_path: pathlib.Path) -> None:
    engine: Engine = create_db_engine(database)
    try:
        with build_session_factory(engine)() as session:
            _source(session, "kev-dark", feed_url=CISA_KEV_FEED_URL, last_success=None)
            session.commit()
    finally:
        engine.dispose()
    assert (
        _run(database, "--out", str(tmp_path), "--sources", "uk-ncsc,kev-dark", "--kev-json") == 1
    )
    assert list(tmp_path.iterdir()) == []
