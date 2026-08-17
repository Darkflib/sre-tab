import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';

import { ApiError } from '../api/client';
import { addBookmark, removeBookmark, setReadState } from '../api/endpoints';
import type { FeedItem, Layout } from '../api/types';
import { FilterBar } from '../components/FilterBar';
import { ItemCard } from '../components/ItemCard';
import { EmptyState, ErrorState, LoadingState, Spinner } from '../components/States';
import { useCatalogue } from '../catalogue/useCatalogue';
import {
  applyFiltersToParams,
  hasOverride,
  parseFilters,
  selectsNothing,
  type FeedFilters,
} from '../feed/filters';
import { useFeed } from '../feed/useFeed';
import { computeShares, findDominantSource } from '../feed/volume';
import { useAuthenticatedSession } from '../session/useSession';

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}

export function FeedPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { preferences } = useAuthenticatedSession();
  const catalogue = useCatalogue();

  const filters = useMemo(() => parseFilters(searchParams), [searchParams]);
  // The card-count preference doubles as the page size: it is exactly the
  // number of items the user said they want in front of them at once.
  const limit = clamp(preferences.max_visible_cards, 1, 100);
  const feed = useFeed(filters, limit);

  const [actionError, setActionError] = useState<ApiError | null>(null);

  const setFilters = useCallback(
    (next: FeedFilters) => {
      setSearchParams(applyFiltersToParams(searchParams, next), { replace: false });
    },
    [searchParams, setSearchParams],
  );

  const shares = useMemo(() => computeShares(feed.entries), [feed.entries]);
  const dominant = findDominantSource(shares, feed.entries.length);

  const { patchEntry } = feed;

  // Optimistic, then reconciled: each mutation changes exactly one field
  // and reverses exactly that field on failure, so overlapping actions on
  // the same card cannot undo one another.
  const applyRead = useCallback(
    (item: FeedItem, read: boolean) => {
      setActionError(null);
      patchEntry(item.id, (entry) => ({ ...entry, read }));
      void setReadState(item.id, read).catch((cause: unknown) => {
        patchEntry(item.id, (entry) => ({ ...entry, read: !read }));
        setActionError(cause instanceof ApiError ? cause : new ApiError(0, 'Unexpected error.'));
      });
    },
    [patchEntry],
  );

  const applyBookmark = useCallback(
    (item: FeedItem, bookmarked: boolean) => {
      setActionError(null);
      patchEntry(item.id, (entry) => ({ ...entry, bookmarked }));
      const work = bookmarked ? addBookmark(item.id) : removeBookmark(item.id);
      void work.catch((cause: unknown) => {
        patchEntry(item.id, (entry) => ({ ...entry, bookmarked: !bookmarked }));
        setActionError(cause instanceof ApiError ? cause : new ApiError(0, 'Unexpected error.'));
      });
    },
    [patchEntry],
  );

  const onOpen = useCallback(
    (item: FeedItem) => {
      if (!item.read) applyRead(item, true);
    },
    [applyRead],
  );

  const onToggleRead = useCallback(
    (item: FeedItem) => {
      applyRead(item, !item.read);
    },
    [applyRead],
  );

  const onToggleBookmark = useCallback(
    (item: FeedItem) => {
      applyBookmark(item, !item.bookmarked);
    },
    [applyBookmark],
  );

  const sentinel = useRef<HTMLDivElement | null>(null);
  const { hasMore, loadingMore, loadMore } = feed;

  useEffect(() => {
    const node = sentinel.current;
    if (!node || !hasMore || loadingMore) return undefined;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) loadMore();
      },
      { rootMargin: '400px 0px' },
    );
    observer.observe(node);
    return () => {
      observer.disconnect();
    };
  }, [hasMore, loadingMore, loadMore]);

  if (catalogue.status === 'loading' && feed.status === 'loading') {
    return <LoadingState label="Loading your feed" />;
  }

  return (
    <div className="feed">
      <h1 className="visually-hidden">Feed</h1>

      {catalogue.status === 'ready' ? (
        <FilterBar
          filters={filters}
          onChange={setFilters}
          shares={shares}
          loadedCount={feed.entries.length}
        />
      ) : null}

      {actionError ? (
        <p className="banner banner--error" role="alert">
          {actionError.message}
        </p>
      ) : null}

      {dominant ? (
        <div className="banner banner--notice">
          <p>
            <strong>{dominant.name}</strong> is {Math.round(dominant.share * 100)}% of what has loaded.
            A feed ordered by publication time favours whoever publishes most.
          </p>
          <span className="banner__actions">
            <button
              type="button"
              className="button button--tiny"
              onClick={() => {
                const current = filters.sources ?? shares.map((entry) => entry.slug);
                setFilters({ ...filters, sources: current.filter((slug) => slug !== dominant.slug) });
              }}
            >
              Hide {dominant.name}
            </button>
            <button
              type="button"
              className="button button--tiny"
              onClick={() => {
                setFilters({ ...filters, sources: [dominant.slug] });
              }}
            >
              Show only {dominant.name}
            </button>
          </span>
        </div>
      ) : null}

      <FeedBody
        feed={feed}
        filters={filters}
        onClearFilters={() => {
          setFilters({ topics: null, sources: null });
        }}
        onOpen={onOpen}
        onToggleRead={onToggleRead}
        onToggleBookmark={onToggleBookmark}
        onFilterSource={(slug) => {
          setFilters({ ...filters, sources: [slug] });
        }}
        onFilterTopic={(slug) => {
          setFilters({ ...filters, topics: [slug] });
        }}
        layout={preferences.layout}
      />

      <div ref={sentinel} className="feed__sentinel" aria-hidden="true" />

      {feed.status === 'ready' && feed.entries.length > 0 ? (
        <div className="feed__more">
          {feed.loadMoreError ? (
            <p className="banner banner--error" role="alert">
              {feed.loadMoreError.message}
            </p>
          ) : null}
          {feed.hasMore ? (
            <button
              type="button"
              className="button button--large"
              onClick={feed.loadMore}
              disabled={feed.loadingMore}
            >
              {feed.loadingMore ? <Spinner label="Loading more items" /> : null}
              {feed.loadingMore ? 'Loading…' : 'Load more'}
            </button>
          ) : (
            <p className="feed__end">That is everything for this selection.</p>
          )}
        </div>
      ) : null}
    </div>
  );
}

interface FeedBodyProps {
  feed: ReturnType<typeof useFeed>;
  filters: FeedFilters;
  onClearFilters: () => void;
  onOpen: (item: FeedItem) => void;
  onToggleRead: (item: FeedItem) => void;
  onToggleBookmark: (item: FeedItem) => void;
  onFilterSource: (slug: string) => void;
  onFilterTopic: (slug: string) => void;
  layout: Layout;
}

function FeedBody({
  feed,
  filters,
  onClearFilters,
  onOpen,
  onToggleRead,
  onToggleBookmark,
  onFilterSource,
  onFilterTopic,
  layout,
}: FeedBodyProps) {
  if (selectsNothing(filters)) {
    return (
      <EmptyState
        title="Nothing selected"
        action={
          <button type="button" className="button" onClick={onClearFilters}>
            Back to my selection
          </button>
        }
      >
        <p>
          You have deselected every source or every topic, so no item can match. Turn something back on
          above.
        </p>
      </EmptyState>
    );
  }

  if (feed.status === 'loading') return <LoadingState label="Loading your feed" />;

  if (feed.status === 'error' && feed.error) {
    return <ErrorState error={feed.error} onRetry={feed.reload} what="the feed" />;
  }

  if (feed.entries.length === 0) {
    return hasOverride(filters) ? (
      <EmptyState
        title="No items match these filters"
        action={
          <button type="button" className="button" onClick={onClearFilters}>
            Clear filters
          </button>
        }
      >
        <p>Widen the selection above, or clear the filters to go back to your saved defaults.</p>
      </EmptyState>
    ) : (
      <EmptyState
        title="No items yet"
        action={
          <>
            <button type="button" className="button" onClick={feed.reload}>
              Check again
            </button>
            <Link className="button button--quiet" to="/settings">
              Review sources
            </Link>
          </>
        }
      >
        <p>
          This is what a brand-new instance looks like — nothing is broken. The server fetches each
          source on its own schedule, from every fifteen minutes to hourly, so the first items appear
          once that cycle has run.
        </p>
        <p>
          If it stays empty for longer than an hour, the operator should check the source refresh status
          on the server.
        </p>
      </EmptyState>
    );
  }

  return (
    <ul className="item-list" data-layout={layout}>
      {feed.entries.map((item) => (
        <li key={item.id}>
          <ItemCard
            item={item}
            onOpen={onOpen}
            onToggleRead={onToggleRead}
            onToggleBookmark={onToggleBookmark}
            onFilterSource={onFilterSource}
            onFilterTopic={onFilterTopic}
          />
        </li>
      ))}
    </ul>
  );
}
