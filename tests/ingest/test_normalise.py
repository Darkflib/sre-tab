"""Sanitisation, canonical-URL normalisation, and the date fallback."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.ingest.normalise import (
    MAX_SUMMARY_LENGTH,
    MAX_TITLE_LENGTH,
    InvalidItemURLError,
    _safe_optional_url,
    normalise_entries,
    normalise_entry,
    normalise_published,
    normalise_url,
    to_plain_text,
)
from app.ingest.parse import ParsedEntry

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def entry(**overrides: object) -> ParsedEntry:
    fields: dict[str, object] = {
        "title": "A title",
        "link": "https://example.org/article",
        "summary": None,
        "published": None,
        "image_url": None,
        "entry_id": None,
    }
    fields.update(overrides)
    return ParsedEntry(**fields)  # type: ignore[arg-type]


# --- sanitisation -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<p>Hello <b>world</b></p>", "Hello world"),
        ("<a href='https://evil.example'>click</a>", "click"),
        ("<img src=x onerror=alert(1)>caption", "caption"),
        ("plain text", "plain text"),
        ("Tom &amp; Jerry", "Tom & Jerry"),
        ("  spaced\n\nout  ", "spaced out"),
        ("<!-- comment -->visible", "visible"),
    ],
)
def test_markup_is_stripped_to_text(raw: str, expected: str) -> None:
    assert to_plain_text(raw) == expected


def test_script_bodies_are_removed_not_merely_unwrapped() -> None:
    assert to_plain_text("<script>alert(1)</script>ok") == "ok"
    assert to_plain_text("<style>body{}</style>ok") == "ok"


def test_double_encoded_markup_cannot_survive() -> None:
    """The trap: stripping escapes, and unescaping reveals markup again."""
    for raw in (
        "&lt;script&gt;alert(1)&lt;/script&gt;",
        "&amp;lt;script&amp;gt;alert(1)&amp;lt;/script&amp;gt;",
        "&#60;script&#62;alert(1)&#60;/script&#62;",
    ):
        cleaned = to_plain_text(raw) or ""
        assert "<" not in cleaned
        assert "script" not in cleaned


def test_control_characters_are_removed() -> None:
    assert to_plain_text("a\x00b\x07c") == "abc"


def test_empty_and_markup_only_become_none() -> None:
    assert to_plain_text(None) is None
    assert to_plain_text("") is None
    assert to_plain_text("   ") is None
    assert to_plain_text("<br/>") is None


def test_field_lengths_are_capped() -> None:
    item = normalise_entry(entry(title="T" * 5000, summary="S" * 20_000), fetched_at=NOW)
    assert item is not None
    assert len(item.title) == MAX_TITLE_LENGTH
    assert item.summary is not None
    assert len(item.summary) == MAX_SUMMARY_LENGTH


# --- canonical urls -----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Scheme and host case, default port, empty path.
        ("HTTPS://Example.ORG/Article", "https://example.org/Article"),
        ("https://example.org:443/a", "https://example.org/a"),
        ("http://example.org:80/a", "http://example.org/a"),
        ("https://example.org", "https://example.org/"),
        ("https://example.org.", "https://example.org/"),
        # Fragments never identify a different article.
        ("https://example.org/a#comments", "https://example.org/a"),
        ("https://example.org/a#", "https://example.org/a"),
        # Tracking parameters.
        ("https://example.org/a?utm_source=rss&utm_medium=feed", "https://example.org/a"),
        ("https://example.org/a?id=7&utm_campaign=x", "https://example.org/a?id=7"),
        ("https://example.org/a?fbclid=abc", "https://example.org/a"),
        ("https://example.org/a?CMP=share_btn", "https://example.org/a"),
        ("https://example.org/a?at_medium=RSS&at_campaign=KARANGA", "https://example.org/a"),
        ("https://example.org/a?source=rss----abc123", "https://example.org/a"),
        # Parameter order must not create a duplicate.
        ("https://example.org/a?b=2&a=1", "https://example.org/a?a=1&b=2"),
        # Dot segments and percent-encoding.
        ("https://example.org/x/../a", "https://example.org/a"),
        ("https://example.org/p%c3%a9", "https://example.org/p%C3%A9"),
        # Non-default ports survive.
        ("https://example.org:8443/a", "https://example.org:8443/a"),
    ],
)
def test_url_normalisation(raw: str, expected: str) -> None:
    assert normalise_url(raw) == expected


def test_trailing_slash_is_preserved() -> None:
    """Deliberate: /a and /a/ are different resources on many servers.

    Under-normalising costs a duplicate card; over-normalising merges two
    genuinely different articles and loses one.
    """
    assert normalise_url("https://example.org/a") != normalise_url("https://example.org/a/")


def test_http_and_https_stay_distinct() -> None:
    assert normalise_url("http://example.org/a") != normalise_url("https://example.org/a")


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",
        "https://user:pass@example.org/a",
        "https://127.0.0.1/a",
        "https://[::1]/a",
        "not a url",
        "https://singlelabel/a",
        "https://example.org/a\nb",
        "https://example.org/" + "a" * 4000,
    ],
)
def test_unusable_item_urls_are_refused(raw: str) -> None:
    with pytest.raises(InvalidItemURLError):
        normalise_url(raw)


def test_an_unusable_link_drops_only_that_entry() -> None:
    assert normalise_entry(entry(link="javascript:alert(1)"), fetched_at=NOW) is None
    assert normalise_entry(entry(link=None), fetched_at=NOW) is None


# --- image urls -----------------------------------------------------------
#
# `image_url` used to be validated more weakly than `canonical_url`: it
# checked scheme, host presence, userinfo, and length, and stopped there.
# A feed could supply an image host that no item URL could ever be — a
# bare name or an IP literal — and `img-src 'self' https: data:` in
# app/middleware.py would have the *operator's browser* fetch it. Both
# paths now share the same host rules via `_feed_url_host`; this section
# is the acceptance table for that.


def test_plain_https_image_url_passes_unchanged() -> None:
    assert _safe_optional_url("https://example.org/cover.png") == "https://example.org/cover.png"


def test_image_url_host_case_is_not_load_bearing() -> None:
    assert _safe_optional_url("HTTPS://Example.ORG/cover.png") == "https://example.org/cover.png"


@pytest.mark.parametrize(
    "raw",
    [
        # Plain IP literals.
        "https://127.0.0.1/cover.png",
        "https://192.168.1.1/cover.png",
        "https://[::1]/cover.png",
        "https://[2606:4700::1111]/cover.png",
        # Obfuscated IPv4 — the exact forms that slipped past IP-literal
        # detection when it ran before host normalisation instead of
        # after it.
        "https://0x7f.0.0.1./cover.png",
        "https://127.1./cover.png",
        "https://0177.1./cover.png",
        "https://0.0.0.0./cover.png",
        # The same obfuscated forms without the trailing dot.
        "https://0x7f.0.0.1/cover.png",
        "https://127.1/cover.png",
        "https://0177.1/cover.png",
        "https://0.0.0.0/cover.png",
        # A bare decimal integer is 127.0.0.1 to inet_aton.
        "https://2130706433/cover.png",
        # A dotless host — no legitimate image link is one.
        "https://singlelabel/cover.png",
        # Credentials.
        "https://user:pass@example.org/cover.png",
        "https://user@example.org/cover.png",
        # A control character embedded in the url.
        "https://example.org/co\tver.png",
        "https://example.org/co\x00ver.png",
    ],
)
def test_hostile_image_urls_are_rejected(raw: str) -> None:
    assert _safe_optional_url(raw) is None


def test_rejected_image_url_still_lets_the_item_normalise() -> None:
    """An optional decorative image degrades to no image, never a dropped item."""
    item = normalise_entry(
        entry(link="https://example.org/article", image_url="https://0x7f.0.0.1/cover.png"),
        fetched_at=NOW,
    )
    assert item is not None
    assert item.canonical_url == "https://example.org/article"
    assert item.image_url is None


# --- published time -----------------------------------------------------


def test_missing_date_falls_back_to_fetch_time() -> None:
    assert normalise_published(None, fetched_at=NOW) == NOW


def test_naive_date_is_treated_as_utc() -> None:
    naive = datetime(2026, 8, 17, 9, 0)  # noqa: DTZ001 - the case under test
    assert normalise_published(naive, fetched_at=NOW) == datetime(2026, 8, 17, 9, 0, tzinfo=UTC)


def test_aware_date_is_converted_to_utc() -> None:
    aware = datetime(2026, 8, 17, 9, 0, tzinfo=UTC) + timedelta(hours=0)
    assert normalise_published(aware, fetched_at=NOW).tzinfo == UTC


def test_absurd_future_date_is_clamped_to_fetch_time() -> None:
    future = datetime(2099, 1, 1, tzinfo=UTC)
    assert normalise_published(future, fetched_at=NOW) == NOW


def test_small_clock_skew_is_tolerated() -> None:
    slightly_ahead = NOW + timedelta(minutes=30)
    assert normalise_published(slightly_ahead, fetched_at=NOW) == slightly_ahead


# --- batches ------------------------------------------------------------


def test_in_batch_duplicates_collapse() -> None:
    entries = (
        entry(link="https://example.org/a?utm_source=x"),
        entry(link="https://example.org/a"),
        entry(link="https://example.org/b"),
    )
    items = normalise_entries(entries, fetched_at=NOW, oldest_allowed=None)
    assert [item.canonical_url for item in items] == [
        "https://example.org/a",
        "https://example.org/b",
    ]


def test_items_outside_the_retention_window_are_dropped() -> None:
    cutoff = NOW - timedelta(days=90)
    entries = (
        entry(link="https://example.org/old", published=cutoff - timedelta(days=1)),
        entry(link="https://example.org/new", published=NOW - timedelta(days=1)),
    )
    items = normalise_entries(entries, fetched_at=NOW, oldest_allowed=cutoff)
    assert [item.canonical_url for item in items] == ["https://example.org/new"]


def test_title_falls_back_to_the_url() -> None:
    item = normalise_entry(entry(title=None, link="https://example.org/a"), fetched_at=NOW)
    assert item is not None
    assert item.title == "https://example.org/a"


def test_image_url_must_be_http() -> None:
    item = normalise_entry(entry(image_url="javascript:alert(1)"), fetched_at=NOW)
    assert item is not None
    assert item.image_url is None
