"""The seed catalogue, and the contracts it has to keep.

The expensive mistake here is silent: rename a slug and every new user
lands on an empty default source selection with nothing logged and no
test failing. Hence the first assertion in this module.
"""

from __future__ import annotations

import pytest

from app.cli.catalogue import SOURCES, TOPICS, InvalidMediumTag, medium_source, slug_problem
from app.cli.operations import validate_feed_url
from app.ingest.topicrules import rule_slugs
from app.services.preferences import DEFAULT_SOURCE_SLUGS


def test_the_seed_supplies_every_default_source_slug() -> None:
    """``DEFAULT_SOURCE_SLUGS`` is intersected with what the instance
    holds, so a slug the seed does not create is silently dropped and the
    user's default selection is empty."""
    seeded = {source.slug for source in SOURCES}
    assert set(DEFAULT_SOURCE_SLUGS) <= seeded


def test_slugs_and_feed_urls_are_unique() -> None:
    assert len({source.slug for source in SOURCES}) == len(SOURCES)
    assert len({source.feed_url for source in SOURCES}) == len(SOURCES)


def test_every_seeded_source_survives_the_config_time_guard() -> None:
    """Acceptance criterion 5 applies to the shipped catalogue too: a
    seed URL the SSRF guard would refuse is a bug in the seed."""
    for source in SOURCES:
        assert validate_feed_url(source.feed_url) == source.feed_url


def test_every_seeded_source_has_topics_the_taxonomy_defines() -> None:
    """An item with no topics is invisible under an explicit ?topics=
    filter, and items inherit their source's topics."""
    known = {slug for slug, _ in TOPICS}
    for source in SOURCES:
        assert source.topics, f"{source.slug} has no default topics"
        assert set(source.topics) <= known


#: The taxonomy as PLAN-v1.md gives it. Pinned separately from the
#: sections group below so that widening the catalogue for a publisher's
#: sections cannot quietly drop one of the originals — which is the half
#: with user rows pointing at it.
PLAN_TOPICS = {
    "webdev",
    "python",
    "devops",
    "security",
    "open-source",
    "ai-ml",
    "hardware",
    "tech-industry",
    "science",
    "uk-news",
    "world-news",
}

#: Added for `app.ingest.topicrules`: a publisher's section is discarded
#: unless the catalogue has a slug to map it onto.
SECTION_TOPICS = {
    "sport",
    "culture",
    "lifestyle",
    "business",
    "politics",
    "society",
    "environment",
    "opinion",
}


def test_the_taxonomy_covers_the_plan() -> None:
    assert {slug for slug, _ in TOPICS} == PLAN_TOPICS | SECTION_TOPICS


def test_slugs_are_unique_and_usable() -> None:
    slugs = [slug for slug, _ in TOPICS]
    assert len(set(slugs)) == len(slugs)
    for slug in slugs:
        assert slug_problem(slug) is None, slug


def test_every_slug_a_rule_can_emit_is_in_the_catalogue() -> None:
    """The ruleset names topics by slug and the link cannot be written
    without a ``topics`` row to point at, so a rule naming a slug the seed
    does not create is a tag that silently never appears. ``_rule_topic_rows``
    logs and skips it rather than failing the refresh, which is the right
    runtime behaviour and exactly why it needs catching here instead."""
    assert rule_slugs() <= {slug for slug, _ in TOPICS}


def test_no_section_maps_to_a_topic_the_rules_alone_invented() -> None:
    """Every section topic has to be reachable, or it is dead weight on
    the onboarding picker. The plan's own topics are exempt: they describe
    sources, and several have no publisher section at all."""
    assert rule_slugs() >= SECTION_TOPICS


def test_refresh_intervals_match_the_plan() -> None:
    intervals = {source.slug: source.refresh_minutes for source in SOURCES}
    assert intervals == {
        "hacker-news": 15,
        "lobsters": 30,
        "dev-to": 30,
        "lwn": 60,
        "ars-technica": 30,
        "bbc-news": 15,
        "guardian-uk": 30,
        "cisa-kev": 60,
    }


# --- the Medium template ------------------------------------------------


def test_a_medium_tag_expands_to_an_ordinary_source() -> None:
    source = medium_source("python")
    assert source.slug == "medium-python"
    assert source.feed_url == "https://medium.com/feed/tag/python"
    assert validate_feed_url(source.feed_url) == source.feed_url


@pytest.mark.parametrize(
    "tag",
    [
        "",
        "   ",
        "../../etc/passwd",
        "python?x=1",
        "python#frag",
        "Python Ops",
        "tag/with/slash",
        "https://evil.example/",
        "trailing-",
        "-leading",
        "double--hyphen",
        "a" * 65,
        "tag%2fescape",
    ],
)
def test_a_hostile_medium_tag_is_refused_at_configuration_time(tag: str) -> None:
    """This is the whole reason the template is expanded by the operator
    rather than at fetch time: the untrusted component never reaches a
    URL that anything later trusts."""
    with pytest.raises(InvalidMediumTag):
        medium_source(tag)
