"""Topics read out of an item's own URL.

The cases that matter are the ones the ruleset must *not* answer. A rule
that over-reaches is worse than no rule: a wrong tag is muted by someone,
and then real items disappear.
"""

from __future__ import annotations

import pytest

from app.ingest.topicrules import SECTIONS, rule_slugs, topics_for_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # The four links that prompted the feature.
        ("https://www.bbc.co.uk/sport/cricket/videos/cq70dnxrg79lo", ("sport",)),
        (
            "https://www.theguardian.com/uk-news/2026/sep/17/nicholas-brandram-putney",
            ("uk-news",),
        ),
        ("https://www.bbc.co.uk/news/articles/cx980qj57r89o", ()),
        ("https://www.bbc.co.uk/news/articles/c68xkypqyxw7o", ()),
        # The rest of the BBC's sport sections, which are the leakage this
        # exists for.
        ("https://www.bbc.co.uk/sport/football/live/c9081nx0q7pt", ("sport",)),
        ("https://www.bbc.co.uk/sport/athletics/articles/cx2k0lz9v1jo", ("sport",)),
        # The Guardian's sections, which are the bulk of the correction.
        ("https://www.theguardian.com/football/2026/sep/17/match-report", ("sport",)),
        ("https://www.theguardian.com/music/2026/sep/17/album-review", ("culture",)),
        ("https://www.theguardian.com/commentisfree/2026/sep/17/column", ("opinion",)),
        ("https://www.theguardian.com/technology/2026/sep/17/chips", ("tech-industry",)),
        ("https://www.theguardian.com/us-news/2026/sep/17/washington", ("world-news",)),
        # Ars, which maps almost entirely onto topics that already existed.
        ("https://arstechnica.com/ai/2026/09/apple-m-series-ultra/", ("ai-ml",)),
        ("https://arstechnica.com/security/2026/09/cyberattack/", ("security",)),
        ("https://arstechnica.com/cars/2026/09/ev-review/", ("hardware",)),
    ],
)
def test_a_section_in_the_path_becomes_a_topic(url: str, expected: tuple[str, ...]) -> None:
    assert topics_for_url(url) == expected


def test_www_is_not_a_second_entry_to_keep_in_step() -> None:
    assert topics_for_url("https://bbc.co.uk/sport/cricket/x") == ("sport",)
    assert topics_for_url("https://www.bbc.co.uk/sport/cricket/x") == ("sport",)


@pytest.mark.parametrize(
    "url",
    [
        # dev.to's first segment is an author, and there are as many of
        # them as there are writers. This is the case that decided the
        # ruleset had to be a lookup rather than "read the first segment".
        "https://dev.to/jrandom/why-i-rewrote-it-in-rust-4b79",
        "https://dev.to/sport/a-post-that-happens-to-be-called-sport-1a2b",
        # An aggregator's own pages, and a host with no rules at all.
        "https://news.ycombinator.com/item?id=41000000",
        "https://lobste.rs/s/abcdef/something",
        "https://lwn.net/Articles/1001234/",
        # A host that merely ends in one that has rules.
        "https://notbbc.co.uk/sport/cricket/x",
        # A subdomain is a different service and is not assumed to share a
        # path vocabulary.
        "https://feeds.bbci.co.uk/sport/cricket/x",
        # Sections nothing maps.
        "https://www.bbc.co.uk/iplayer/episode/m002abcd",
        "https://www.bbc.co.uk/weather/2643743",
        # No path to read.
        "https://www.theguardian.com/",
        "https://www.theguardian.com",
    ],
)
def test_a_url_with_no_section_earns_no_topics(url: str) -> None:
    assert topics_for_url(url) == ()


@pytest.mark.parametrize(
    "url",
    ["", "not a url", "http://", "https:///sport/cricket", "ftp://bbc.co.uk/sport/x"],
)
def test_an_unusable_url_returns_nothing_rather_than_raising(url: str) -> None:
    """Ingest normalises before storing, so these cannot arrive that way.
    The re-tag pass reads rows written by earlier versions and by hand,
    and one bad row must not stop it."""
    assert topics_for_url(url) == ()


def test_the_section_lookup_is_case_folded() -> None:
    assert topics_for_url("https://www.BBC.co.uk/Sport/cricket/x") == ("sport",)


def test_every_host_key_is_already_normalised() -> None:
    """A key with a ``www.`` or a capital could never match, because
    :func:`topics_for_url` strips and folds before the lookup — so it
    would be a rule that silently does nothing."""
    for host in SECTIONS:
        assert host == host.lower()
        assert not host.startswith("www.")


def test_every_section_key_is_already_normalised() -> None:
    for sections in SECTIONS.values():
        for section in sections:
            assert section == section.lower()
            assert "/" not in section


def test_no_rule_emits_a_duplicate_slug() -> None:
    for sections in SECTIONS.values():
        for section, slugs in sections.items():
            assert len(set(slugs)) == len(slugs), section


def test_rule_slugs_is_the_union_of_what_the_rules_emit() -> None:
    assert rule_slugs() == {
        slug for sections in SECTIONS.values() for slugs in sections.values() for slug in slugs
    }
