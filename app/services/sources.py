"""Source and topic catalogue for the settings and onboarding screens."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.v1.schemas import SourceOut, SourcesResponse, TopicOut
from app.db.models import Source, Topic


def list_catalogue(db: Session) -> SourcesResponse:
    """Enabled sources with their default topic slugs, plus enabled topics.

    Disabled rows are withheld from both lists on purpose: the settings UI
    is built from this response, and
    :func:`app.services.preferences.apply_patch` rejects a disabled slug,
    so offering one would render a control that can only 422.
    """
    sources = db.scalars(
        select(Source)
        .where(Source.enabled.is_(True))
        # Source.topics is lazy="raise"; the eager load is what keeps this
        # one query per relationship rather than one per source.
        .options(selectinload(Source.topics))
        .order_by(Source.name, Source.slug)
    ).all()
    topics = db.scalars(
        select(Topic).where(Topic.enabled.is_(True)).order_by(Topic.name, Topic.slug)
    ).all()

    return SourcesResponse(
        sources=[
            SourceOut(
                slug=source.slug,
                name=source.name,
                feed_url=source.feed_url,
                website_url=source.website_url,
                icon_url=source.icon_url,
                refresh_minutes=source.refresh_minutes,
                enabled=source.enabled,
                topics=sorted(topic.slug for topic in source.topics),
            )
            for source in sources
        ],
        topics=[
            TopicOut(slug=topic.slug, name=topic.name, enabled=topic.enabled) for topic in topics
        ],
    )
