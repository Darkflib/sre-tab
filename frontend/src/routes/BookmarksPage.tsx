import { useCallback, useState } from 'react';
import { Link } from 'react-router-dom';

import { ApiError } from '../api/client';
import { removeBookmark, setReadState } from '../api/endpoints';
import type { FeedItem } from '../api/types';
import { ItemCard } from '../components/ItemCard';
import { EmptyState, ErrorState, LoadingState, Spinner } from '../components/States';
import { useBookmarks } from '../feed/useFeed';
import { useAuthenticatedSession } from '../session/useSession';

export function BookmarksPage() {
  const { preferences } = useAuthenticatedSession();
  const bookmarks = useBookmarks(Math.min(100, Math.max(1, preferences.max_visible_cards)));
  const [actionError, setActionError] = useState<ApiError | null>(null);

  const { patchEntry, removeEntry } = bookmarks;

  const onRemove = useCallback(
    (item: FeedItem) => {
      setActionError(null);
      removeEntry(item.id);
      void removeBookmark(item.id).catch((cause: unknown) => {
        setActionError(cause instanceof ApiError ? cause : new ApiError(0, 'Unexpected error.'));
        // The list is authoritative on the server; refetch rather than
        // guess where the row belonged.
        bookmarks.reload();
      });
    },
    [bookmarks, removeEntry],
  );

  const onSetRead = useCallback(
    (itemId: number, read: boolean) => {
      setActionError(null);
      patchEntry(itemId, (entry) => ({ ...entry, item: { ...entry.item, read } }));
      void setReadState(itemId, read).catch((cause: unknown) => {
        patchEntry(itemId, (entry) => ({ ...entry, item: { ...entry.item, read: !read } }));
        setActionError(cause instanceof ApiError ? cause : new ApiError(0, 'Unexpected error.'));
      });
    },
    [patchEntry],
  );

  return (
    <div className="bookmarks">
      <h1>Bookmarks</h1>

      {actionError ? (
        <p className="banner banner--error" role="alert">
          {actionError.message}
        </p>
      ) : null}

      {bookmarks.status === 'loading' ? <LoadingState label="Loading your bookmarks" /> : null}

      {bookmarks.status === 'error' && bookmarks.error ? (
        <ErrorState error={bookmarks.error} onRetry={bookmarks.reload} what="your bookmarks" />
      ) : null}

      {bookmarks.status === 'ready' && bookmarks.entries.length === 0 ? (
        <EmptyState
          title="No bookmarks yet"
          action={
            <Link className="button" to="/feed">
              Go to the feed
            </Link>
          }
        >
          <p>Bookmark anything in the feed and it will be kept here until you remove it.</p>
        </EmptyState>
      ) : null}

      {bookmarks.entries.length > 0 ? (
        <ul className="item-list" data-layout={preferences.layout}>
          {bookmarks.entries.map((entry) => (
            <li key={entry.item.id}>
              <ItemCard
                item={{ ...entry.item, bookmarked: true }}
                onOpen={(item) => {
                  if (!item.read) onSetRead(item.id, true);
                }}
                onToggleRead={(item) => {
                  onSetRead(item.id, !item.read);
                }}
                onToggleBookmark={onRemove}
                onRemoveBookmark={onRemove}
              />
            </li>
          ))}
        </ul>
      ) : null}

      {bookmarks.hasMore ? (
        <div className="feed__more">
          {bookmarks.loadMoreError ? (
            <p className="banner banner--error" role="alert">
              {bookmarks.loadMoreError.message}
            </p>
          ) : null}
          <button
            type="button"
            className="button button--large"
            onClick={bookmarks.loadMore}
            disabled={bookmarks.loadingMore}
          >
            {bookmarks.loadingMore ? <Spinner label="Loading more bookmarks" /> : null}
            {bookmarks.loadingMore ? 'Loading…' : 'Load more'}
          </button>
        </div>
      ) : null}
    </div>
  );
}
