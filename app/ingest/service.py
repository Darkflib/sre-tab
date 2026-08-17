"""Per-source refresh, isolated from its neighbours.

:meth:`IngestService.refresh_source` does not raise. Every failure —
DNS, TLS, timeout, oversized body, hostile XML, a database error — is
caught, classified, recorded against that source alone, and logged. One
source failing cannot stop the tick, cannot touch another source's
items, and cannot delete anything: the write path is insert-or-ignore
and nothing in a failure branch deletes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Source, SourceTopic
from app.ingest.errors import IngestError
from app.ingest.fetch import FeedFetcher, FetchResult
from app.ingest.normalise import NormalisedItem, normalise_entries
from app.ingest.parse import parse_feed
from app.ingest.status import SourceStatus, SourceStatusRegistry
from app.ingest.store import prune_feed_items, upsert_items
from app.settings import Settings

log = structlog.get_logger("app.ingest")


@dataclass(frozen=True)
class SourceRef:
    """A source snapshotted out of the session before any network work."""

    id: int
    slug: str
    feed_url: str
    refresh_minutes: int
    topic_ids: tuple[int, ...]


class IngestService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        *,
        fetcher: FeedFetcher | None = None,
        status: SourceStatusRegistry | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._fetcher = fetcher or FeedFetcher(settings)
        self.status = status or SourceStatusRegistry()

    # -- reads -----------------------------------------------------------

    def enabled_sources(self) -> list[SourceRef]:
        with self._session_factory() as session:
            sources = list(session.scalars(select(Source).where(Source.enabled.is_(True))))
            topic_map: dict[int, list[int]] = {}
            for source_id, topic_id in session.execute(
                select(SourceTopic.source_id, SourceTopic.topic_id).where(
                    SourceTopic.source_id.in_([source.id for source in sources])
                )
            ):
                topic_map.setdefault(source_id, []).append(topic_id)
            return [
                SourceRef(
                    id=source.id,
                    slug=source.slug,
                    feed_url=source.feed_url,
                    refresh_minutes=source.refresh_minutes,
                    topic_ids=tuple(sorted(topic_map.get(source.id, []))),
                )
                for source in sources
            ]

    def due_sources(self, *, now: datetime | None = None) -> list[SourceRef]:
        moment = now or datetime.now(UTC)
        return [
            source for source in self.enabled_sources() if self.status.is_due(source.id, now=moment)
        ]

    # -- writes ----------------------------------------------------------

    def refresh_source(self, source: SourceRef, *, now: datetime | None = None) -> SourceStatus:
        """Refresh one source. Never raises."""
        moment = now or datetime.now(UTC)
        bound = log.bind(source_id=source.id, source_slug=source.slug)
        try:
            result = self._fetch(source)
            parsed = parse_feed(result.content)
            items = normalise_entries(
                parsed.entries,
                fetched_at=moment,
                oldest_allowed=self._retention_cutoff(moment),
            )
            inserted = self._store(source, items, fetched_at=moment)
        except IngestError as exc:
            return self._fail(source, bound, exc.error_class, str(exc), moment)
        except Exception as exc:
            # Deliberately broad: per-source isolation is the point.
            return self._fail(source, bound, type(exc).__name__, str(exc), moment)

        status = self.status.record_success(
            source_id=source.id,
            slug=source.slug,
            refresh_minutes=source.refresh_minutes,
            item_count=len(items),
            inserted_count=inserted,
            now=moment,
        )
        bound.info(
            "source_refresh_succeeded",
            feed_version=parsed.version,
            entries=len(parsed.entries),
            usable_items=len(items),
            inserted=inserted,
            redirect_hops=len(result.hops) - 1,
        )
        return status

    def refresh_all(self, sources: Sequence[SourceRef] | None = None) -> list[SourceStatus]:
        """Refresh each source in turn, isolated from the others."""
        targets = list(sources) if sources is not None else self.due_sources()
        return [self.refresh_source(source) for source in targets]

    def prune(self, *, now: datetime | None = None) -> int:
        moment = now or datetime.now(UTC)
        cutoff = self._retention_cutoff(moment)
        with self._session_factory() as session:
            return prune_feed_items(session, cutoff=cutoff)

    # -- internals -------------------------------------------------------

    def _retention_cutoff(self, now: datetime) -> datetime:
        return now - timedelta(days=self._settings.feed_retention_days)

    def _fetch(self, source: SourceRef) -> FetchResult:
        # The allow-list is exactly this source's configured URL, read
        # from the database on every refresh. Nothing else is fetchable.
        return self._fetcher.fetch(source.feed_url, allowed_urls={source.feed_url})

    def _store(
        self, source: SourceRef, items: Sequence[NormalisedItem], *, fetched_at: datetime
    ) -> int:
        with self._session_factory() as session:
            return upsert_items(
                session,
                source_id=source.id,
                items=items,
                topic_ids=source.topic_ids,
                fetched_at=fetched_at,
            )

    def _fail(
        self,
        source: SourceRef,
        bound: structlog.stdlib.BoundLogger,
        error_class: str,
        detail: str,
        moment: datetime,
    ) -> SourceStatus:
        status = self.status.record_failure(
            source_id=source.id,
            slug=source.slug,
            refresh_minutes=source.refresh_minutes,
            error_class=error_class,
            detail=detail,
            now=moment,
        )
        bound.warning(
            "source_refresh_failed",
            error_class=error_class,
            detail=detail,
            consecutive_failures=status.consecutive_failures,
            next_due_at=status.next_due_at.isoformat() if status.next_due_at else None,
        )
        return status
