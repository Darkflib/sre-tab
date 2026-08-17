"""The preference service — agent A's ``me.py`` is written against these.

Tested at the service boundary rather than over HTTP because the routes
that call them belong to agent A and still answer 501.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.v1.schemas import PreferencesPatch
from app.db.models import Layout, Theme, User, UserPreferences, UserPreferenceSource
from app.services import preferences as service
from app.services.preferences import DEFAULT_SOURCE_SLUGS
from tests.api.conftest import Catalogue


def _profile_rows(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(UserPreferences)) or 0


def test_ensure_profile_creates_the_instance_defaults(
    db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    service.ensure_profile(db_session, test_user)
    db_session.commit()

    profile = service.load_profile(db_session, test_user)

    assert profile.theme is Theme.SYSTEM
    assert profile.layout is Layout.GRID
    assert profile.max_visible_cards == 25
    assert profile.onboarding_completed is False


def test_ensure_profile_is_idempotent(
    db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    service.ensure_profile(db_session, test_user)
    db_session.commit()
    before = service.load_profile(db_session, test_user)

    service.ensure_profile(db_session, test_user)
    service.ensure_profile(db_session, test_user)
    db_session.commit()

    assert service.load_profile(db_session, test_user) == before
    assert _profile_rows(db_session) == 1


def test_ensure_profile_does_not_resurrect_a_cleared_selection(
    db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    """A second sign-in must not undo the user's settings."""
    service.ensure_profile(db_session, test_user)
    service.apply_patch(db_session, test_user, PreferencesPatch(sources=[], topics=[]))
    db_session.commit()

    service.ensure_profile(db_session, test_user)
    db_session.commit()

    profile = service.load_profile(db_session, test_user)
    assert profile.sources == []
    assert profile.topics == []


def test_defaults_do_not_enable_every_source(
    db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    """The volume-asymmetry constraint, as an assertion.

    BBC News publishes at many times the rate of Lobsters or LWN, so a
    default with everything on is a general-news feed within the hour and
    the low-volume sources never surface.
    """
    service.ensure_profile(db_session, test_user)
    db_session.commit()

    profile = service.load_profile(db_session, test_user)

    assert "bbc-news" not in profile.sources
    assert set(profile.sources) < {"bbc-news", "hacker-news", "lobsters"}
    assert set(profile.sources) <= set(DEFAULT_SOURCE_SLUGS)


def test_defaults_enable_the_developer_core_that_the_instance_has(
    db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    service.ensure_profile(db_session, test_user)
    db_session.commit()

    profile = service.load_profile(db_session, test_user)

    assert profile.sources == ["hacker-news", "lobsters"]


def test_defaults_never_select_a_disabled_row(
    db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    service.ensure_profile(db_session, test_user)
    db_session.commit()

    profile = service.load_profile(db_session, test_user)

    assert "retired" not in profile.sources
    assert "legacy" not in profile.topics


def test_defaults_select_every_enabled_topic(
    db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    service.ensure_profile(db_session, test_user)
    db_session.commit()

    profile = service.load_profile(db_session, test_user)

    assert profile.topics == ["python", "uk-news", "webdev"]


def test_load_profile_tolerates_a_missing_row(
    db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    """Agent A may call ``load_profile`` on a user that predates the
    profile write; that is a default profile, not a 500."""
    profile = service.load_profile(db_session, test_user)

    assert profile.theme is Theme.SYSTEM


def test_patch_updates_only_the_fields_supplied(
    db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    service.ensure_profile(db_session, test_user)
    db_session.commit()
    before = service.load_profile(db_session, test_user)

    after = service.apply_patch(
        db_session, test_user, PreferencesPatch(theme=Theme.DARK, max_visible_cards=10)
    )
    db_session.commit()

    assert after.theme is Theme.DARK
    assert after.max_visible_cards == 10
    # Untouched by an absent field, including the collections.
    assert after.layout is before.layout
    assert after.onboarding_completed is before.onboarding_completed
    assert after.topics == before.topics
    assert after.sources == before.sources


def test_patch_of_a_single_collection_leaves_the_other_alone(
    db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    service.ensure_profile(db_session, test_user)
    db_session.commit()

    after = service.apply_patch(db_session, test_user, PreferencesPatch(topics=["webdev"]))
    db_session.commit()

    assert after.topics == ["webdev"]
    assert after.sources == ["hacker-news", "lobsters"]


def test_patch_with_an_explicit_empty_list_clears_the_selection(
    db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    """``[]`` is not the same as absent — one clears, the other is a
    no-op, and the whole point of a partial update is telling them
    apart."""
    service.ensure_profile(db_session, test_user)
    db_session.commit()

    after = service.apply_patch(db_session, test_user, PreferencesPatch(sources=[]))
    db_session.commit()

    assert after.sources == []
    assert after.topics != []
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(UserPreferenceSource)
            .where(UserPreferenceSource.user_id == test_user.id)
        )
        == 0
    )


def test_patch_replaces_rather_than_appends(
    db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    service.ensure_profile(db_session, test_user)
    db_session.commit()

    service.apply_patch(db_session, test_user, PreferencesPatch(sources=["bbc-news"]))
    after = service.apply_patch(db_session, test_user, PreferencesPatch(sources=["lobsters"]))
    db_session.commit()

    assert after.sources == ["lobsters"]


def test_patch_tolerates_a_repeated_slug(
    db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    after = service.apply_patch(
        db_session, test_user, PreferencesPatch(sources=["lobsters", "lobsters"])
    )
    db_session.commit()

    assert after.sources == ["lobsters"]


@pytest.mark.parametrize(
    "patch",
    [
        PreferencesPatch(topics=["no-such-topic"]),
        PreferencesPatch(sources=["no-such-source"]),
        # Disabled rows are rejected as firmly as absent ones: the
        # settings screen never offers them, so asking for one is a bug.
        PreferencesPatch(topics=["legacy"]),
        PreferencesPatch(sources=["retired"]),
        PreferencesPatch(topics=["webdev", "no-such-topic"]),
    ],
)
def test_patch_rejects_unknown_or_disabled_slugs(
    db_session: Session, test_user: User, catalogue: Catalogue, patch: PreferencesPatch
) -> None:
    """``ValueError`` specifically — agent A maps that to 422."""
    with pytest.raises(ValueError, match="unknown or disabled"):
        service.apply_patch(db_session, test_user, patch)
    db_session.rollback()


def test_a_rejected_patch_applies_nothing(
    db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    """The valid half of a bad patch must not land."""
    service.ensure_profile(db_session, test_user)
    db_session.commit()

    with pytest.raises(ValueError, match="unknown or disabled"):
        service.apply_patch(
            db_session,
            test_user,
            PreferencesPatch(theme=Theme.DARK, sources=["no-such-source"]),
        )
    db_session.rollback()

    assert service.load_profile(db_session, test_user).theme is Theme.SYSTEM


def test_the_service_does_not_commit(
    db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    """The transaction boundary belongs to the caller.

    Agent A's route commits once, after ``apply_patch`` returns; if the
    service committed on its own, a later failure in that route could not
    be rolled back.
    """
    service.ensure_profile(db_session, test_user)
    db_session.commit()

    service.apply_patch(
        db_session, test_user, PreferencesPatch(theme=Theme.DARK, sources=["bbc-news"])
    )
    db_session.rollback()

    profile = service.load_profile(db_session, test_user)
    assert profile.theme is Theme.SYSTEM
    assert profile.sources == ["hacker-news", "lobsters"]


def test_ensure_profile_does_not_commit(
    db_session: Session, test_user: User, catalogue: Catalogue
) -> None:
    service.ensure_profile(db_session, test_user)
    db_session.rollback()

    assert _profile_rows(db_session) == 0
