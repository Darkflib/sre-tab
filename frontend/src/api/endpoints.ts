/**
 * Thin typed wrappers over the twelve contract endpoints. Screens call
 * these; nothing else imports `client.ts` directly.
 */
import { api, toApiError, unwrap, unwrapEmpty } from './client';
import type {
  BookmarkEntry,
  BookmarkPage,
  FeedPage,
  MeResponse,
  Preferences,
  PreferencesPatch,
  ReadState,
  SourcesResponse,
} from './types';

/** Server-side entry point for GitHub OAuth; a full navigation, not fetch. */
export const GITHUB_SIGN_IN_PATH = '/api/v1/auth/github/start';

export interface FeedQuery {
  topics?: string[];
  sources?: string[];
  cursor?: string;
  limit?: number;
  signal?: AbortSignal;
}

async function guard<T>(work: () => Promise<T>): Promise<T> {
  try {
    return await work();
  } catch (cause) {
    throw toApiError(cause);
  }
}

export function fetchMe(signal?: AbortSignal): Promise<MeResponse> {
  return guard(async () => unwrap(await api.GET('/api/v1/me', { signal })));
}

export function fetchSources(signal?: AbortSignal): Promise<SourcesResponse> {
  return guard(async () => unwrap(await api.GET('/api/v1/sources', { signal })));
}

export function fetchFeed(query: FeedQuery = {}): Promise<FeedPage> {
  const { signal, ...params } = query;
  return guard(async () =>
    unwrap(
      await api.GET('/api/v1/feed', {
        // Omitting `topics`/`sources` is meaningful: the server then uses
        // the user's saved selection. Never send empty arrays by accident.
        params: { query: params },
        signal,
      }),
    ),
  );
}

export function fetchBookmarks(
  query: { cursor?: string; limit?: number; signal?: AbortSignal } = {},
): Promise<BookmarkPage> {
  const { signal, ...params } = query;
  return guard(async () =>
    unwrap(await api.GET('/api/v1/bookmarks', { params: { query: params }, signal })),
  );
}

export function patchPreferences(patch: PreferencesPatch): Promise<Preferences> {
  return guard(async () => unwrap(await api.PATCH('/api/v1/me/preferences', { body: patch })));
}

export function setReadState(itemId: number, read: boolean): Promise<ReadState> {
  return guard(async () =>
    unwrap(
      await api.PUT('/api/v1/items/{item_id}/read-state', {
        params: { path: { item_id: itemId } },
        body: { read },
      }),
    ),
  );
}

export function addBookmark(itemId: number): Promise<BookmarkEntry> {
  return guard(async () =>
    unwrap(
      await api.PUT('/api/v1/items/{item_id}/bookmark', {
        params: { path: { item_id: itemId } },
      }),
    ),
  );
}

export function removeBookmark(itemId: number): Promise<void> {
  return guard(async () => {
    unwrapEmpty(
      await api.DELETE('/api/v1/items/{item_id}/bookmark', {
        params: { path: { item_id: itemId } },
      }),
    );
  });
}

export function logout(): Promise<void> {
  return guard(async () => {
    unwrapEmpty(await api.POST('/api/v1/auth/logout', {}));
  });
}

export function deleteAccount(): Promise<void> {
  return guard(async () => {
    unwrapEmpty(await api.DELETE('/api/v1/me', {}));
  });
}
