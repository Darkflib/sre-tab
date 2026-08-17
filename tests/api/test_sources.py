"""GET /sources — the settings and onboarding catalogue."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.api.conftest import Catalogue


def test_lists_enabled_sources_with_their_topic_slugs(
    authed_client: TestClient, catalogue: Catalogue
) -> None:
    payload = authed_client.get("/api/v1/sources").json()
    by_slug = {source["slug"]: source for source in payload["sources"]}

    assert set(by_slug) == {"bbc-news", "hacker-news", "lobsters"}
    assert by_slug["hacker-news"]["topics"] == ["webdev"]
    assert by_slug["hacker-news"]["name"] == "Hacker News"
    assert by_slug["hacker-news"]["feed_url"] == "https://hacker-news.example/rss"
    assert by_slug["hacker-news"]["refresh_minutes"] == 30


def test_withholds_disabled_sources(authed_client: TestClient, catalogue: Catalogue) -> None:
    payload = authed_client.get("/api/v1/sources").json()

    assert "retired" not in {source["slug"] for source in payload["sources"]}


def test_withholds_disabled_topics(authed_client: TestClient, catalogue: Catalogue) -> None:
    """A disabled topic offered here would render a settings control that
    can only ever 422 — ``apply_patch`` rejects it."""
    payload = authed_client.get("/api/v1/sources").json()

    assert {topic["slug"] for topic in payload["topics"]} == {"webdev", "python", "uk-news"}
    assert all(topic["enabled"] for topic in payload["topics"])


def test_ordering_is_stable(authed_client: TestClient, catalogue: Catalogue) -> None:
    payload = authed_client.get("/api/v1/sources").json()

    assert [source["name"] for source in payload["sources"]] == [
        "BBC News",
        "Hacker News",
        "Lobsters",
    ]
