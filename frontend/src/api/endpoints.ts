/**
 * Thin typed wrappers over the contract endpoints. Screens call these;
 * nothing else imports `client.ts` directly.
 */
import { api, toApiError, unwrap, unwrapEmpty } from './client';
import type {
  ApiToken,
  ApiTokenCreate,
  ApiTokenCreated,
  BookmarkEntry,
  BookmarkPage,
  FeedPage,
  MeResponse,
  Preferences,
  PreferencesPatch,
  ReadFilter,
  ReadState,
  SourcesResponse,
} from './types';

/** Server-side entry point for GitHub OAuth; a full navigation, not fetch. */
export const GITHUB_SIGN_IN_PATH = '/api/v1/auth/github/start';

export interface FeedQuery {
  topics?: string[];
  sources?: string[];
  /** Omitted means `all`; the server's default and the client's agree. */
  read_state?: ReadFilter;
  /** Full-text search over title and summary. Omitted means no narrowing. */
  q?: string;
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


/**
 * The API-token endpoints.
 *
 * `createApiToken` is the only call in this file whose response carries a
 * credential. It is returned to the caller and never stored: the screen
 * shows it once and drops it on the next render, because the server keeps
 * only a hash and cannot produce it again.
 */
export function fetchApiTokens(signal?: AbortSignal): Promise<ApiToken[]> {
  return guard(async () => unwrap(await api.GET('/api/v1/me/tokens', { signal })).tokens);
}

export function createApiToken(body: ApiTokenCreate): Promise<ApiTokenCreated> {
  return guard(async () => unwrap(await api.POST('/api/v1/me/tokens', { body })));
}

export function revokeApiToken(tokenId: number): Promise<void> {
  return guard(async () => {
    unwrapEmpty(
      await api.DELETE('/api/v1/me/tokens/{token_id}', {
        params: { path: { token_id: tokenId } },
      }),
    );
  });
}
