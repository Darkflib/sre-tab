import { fetchBookmarks, fetchFeed } from '../api/endpoints';
import type { BookmarkEntry, FeedItem } from '../api/types';
import { usePagedResource, type PagedResource } from '../data/usePagedResource';
import { filterKey, selectsNothing, type FeedFilters } from './filters';

export function useFeed(filters: FeedFilters, limit: number): PagedResource<FeedItem> {
  return usePagedResource<FeedItem>({
    key: filterKey(filters, limit),
    // Deselecting every source or topic can match nothing; say so locally
    // rather than asking the server a question with no answer.
    enabled: !selectsNothing(filters),
    idOf: (item) => item.id,
    fetchPage: async (cursor, signal) => {
      const page = await fetchFeed({
        // Omitted, not empty: the server then applies the saved selection.
        topics: filters.topics ?? undefined,
        sources: filters.sources ?? undefined,
        // Same reasoning, different default: `all` is what an absent
        // parameter means, so sending it would be noise on the wire.
        read_state: filters.readState === 'all' ? undefined : filters.readState,
        // Ditto: an empty search means no narrowing, which is what an
        // absent parameter already means.
        q: filters.query === '' ? undefined : filters.query,
        cursor,
        limit,
        signal,
      });
      return { entries: page.items, nextCursor: page.next_cursor };
    },
  });
}

export function useBookmarks(limit: number): PagedResource<BookmarkEntry> {
  return usePagedResource<BookmarkEntry>({
    key: `bookmarks|${limit}`,
    enabled: true,
    idOf: (entry) => entry.item.id,
    fetchPage: async (cursor, signal) => {
      const page = await fetchBookmarks({ cursor, limit, signal });
      return { entries: page.bookmarks, nextCursor: page.next_cursor };
    },
  });
}
