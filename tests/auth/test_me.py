"""``/me``: profile read, preference patch, and account deletion."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Bookmark,
    FeedItem,
    Source,
    Topic,
    User,
    UserPreferences,
    UserPreferenceSource,
    UserPreferenceTopic,
    UserReadItem,
    UserSession,
)
from app.settings import Settings
from tests.auth.conftest import ALLOWED_GITHUB_ID, PreferencesStub, csrf_headers

ME_PATH = "/api/v1/me"
PREFERENCES_PATH = "/api/v1/me/preferences"


def test_get_me_returns_the_user_and_the_loaded_profile(
    signed_in_client: TestClient, preferences_stub: PreferencesStub, db_session: Session
) -> None:
    response = signed_in_client.get(ME_PATH)

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["github_id"] == ALLOWED_GITHUB_ID
    assert body["user"]["github_login"] == "octocat"
    assert body["preferences"]["theme"] == "system"

    user_id = db_session.execute(select(User.id)).scalar_one()
    assert preferences_stub.loaded == [user_id]


def test_get_me_never_exposes_session_material(
    signed_in_client: TestClient, preferences_stub: PreferencesStub, settings: Settings
) -> None:
    raw_token = signed_in_client.cookies[settings.session_cookie_name]
    body = signed_in_client.get(ME_PATH).text
    assert raw_token not in body
    assert "token" not in body


def test_patch_preferences_delegates_and_persists(
    signed_in_client: TestClient, preferences_stub: PreferencesStub, settings: Settings
) -> None:
    response = signed_in_client.patch(
        PREFERENCES_PATH,
        json={"theme": "dark", "max_visible_cards": 40},
        headers=csrf_headers(signed_in_client, settings),
    )

    assert response.status_code == 200
    assert response.json()["theme"] == "dark"
    assert response.json()["max_visible_cards"] == 40
    assert [patch.theme for patch in preferences_stub.patches] == ["dark"]


def test_patch_preferences_maps_service_value_error_to_422(
    signed_in_client: TestClient, preferences_stub: PreferencesStub, settings: Settings
) -> None:
    """Unknown or disabled slugs are the service's ``ValueError``; the
    route's job is to turn that into 422 rather than a 500."""
    preferences_stub.patch_error = "unknown topic slug: 'nope'"

    response = signed_in_client.patch(
        PREFERENCES_PATH,
        json={"topics": ["nope"]},
        headers=csrf_headers(signed_in_client, settings),
    )

    assert response.status_code == 422
    assert "nope" in response.json()["detail"]


def _populate_user_owned_rows(db: Session, user: User) -> None:
    topic = Topic(slug="python", name="Python")
    source = Source(
        slug="lobsters",
        name="Lobsters",
        feed_url="https://lobste.rs/rss",
        website_url="https://lobste.rs/",
    )
    db.add_all([topic, source])
    db.flush()

    item = FeedItem(
        source_id=source.id,
        canonical_url="https://example.org/a",
        title="An item",
        published_at=datetime.now(UTC),
    )
    db.add(item)
    db.flush()

    db.add_all(
        [
            UserPreferences(user_id=user.id),
            UserPreferenceTopic(user_id=user.id, topic_id=topic.id),
            UserPreferenceSource(user_id=user.id, source_id=source.id),
            UserReadItem(user_id=user.id, feed_item_id=item.id),
            Bookmark(user_id=user.id, feed_item_id=item.id),
        ]
    )
    db.commit()


def test_delete_me_cascades_every_user_owned_row(
    signed_in_client: TestClient,
    preferences_stub: PreferencesStub,
    settings: Settings,
    db_session: Session,
) -> None:
    """SQLite only honours ``ondelete="CASCADE"`` with
    ``PRAGMA foreign_keys=ON``, which the engine sets on connect. Asserting
    the rows are gone is what keeps that from silently regressing into
    orphaned data."""
    user = db_session.execute(select(User)).scalar_one()
    _populate_user_owned_rows(db_session, user)

    response = signed_in_client.delete(ME_PATH, headers=csrf_headers(signed_in_client, settings))
    assert response.status_code == 204

    for model in (
        User,
        UserSession,
        UserPreferences,
        UserPreferenceTopic,
        UserPreferenceSource,
        UserReadItem,
        Bookmark,
    ):
        remaining = db_session.execute(select(func.count()).select_from(model)).scalar_one()
        assert remaining == 0, f"{model.__tablename__} rows survived the cascade"

    # Shared catalogue data is not user-owned and must survive.
    assert db_session.execute(select(func.count()).select_from(Topic)).scalar_one() == 1
    assert db_session.execute(select(func.count()).select_from(Source)).scalar_one() == 1
    assert db_session.execute(select(func.count()).select_from(FeedItem)).scalar_one() == 1


def test_delete_me_signs_the_browser_out(
    signed_in_client: TestClient, preferences_stub: PreferencesStub, settings: Settings
) -> None:
    assert (
        signed_in_client.delete(
            ME_PATH, headers=csrf_headers(signed_in_client, settings)
        ).status_code
        == 204
    )
    assert not signed_in_client.cookies.get(settings.session_cookie_name)
    assert signed_in_client.get(ME_PATH).status_code == 401


def test_me_requires_a_session(client: TestClient) -> None:
    assert client.get(ME_PATH).status_code == 401
    assert client.patch(PREFERENCES_PATH, json={}).status_code == 401
    assert client.delete(ME_PATH).status_code == 401
