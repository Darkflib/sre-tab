import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError } from '../api/client';

export interface Page<T> {
  entries: T[];
  /** Opaque; passed back verbatim. Never parsed or constructed here. */
  nextCursor: string | null;
}

export type PagedStatus = 'loading' | 'ready' | 'error';

export interface PagedResource<T> {
  entries: T[];
  status: PagedStatus;
  error: ApiError | null;
  /** Set when a *subsequent* page failed; the loaded entries still stand. */
  loadMoreError: ApiError | null;
  hasMore: boolean;
  loadingMore: boolean;
  loadMore: () => void;
  reload: () => void;
  /**
   * Apply a change to one entry against whatever it currently holds, not
   * against a snapshot captured when the handler was created — two quick
   * actions on the same card must not clobber each other.
   */
  patchEntry: (id: number, update: (current: T) => T) => void;
  removeEntry: (id: number) => void;
}

interface Options<T> {
  /** Changing this discards the loaded pages and starts again. */
  key: string;
  /** When false, the resource resolves to an empty, non-erroring list. */
  enabled: boolean;
  idOf: (entry: T) => number;
  fetchPage: (cursor: string | undefined, signal: AbortSignal) => Promise<Page<T>>;
}

interface State<T> {
  /** The cache key these entries belong to; stale responses are dropped. */
  key: string;
  entries: T[];
  status: PagedStatus;
  error: ApiError | null;
  loadMoreError: ApiError | null;
  nextCursor: string | null;
  loadingMore: boolean;
}

function freshState<T>(key: string, enabled: boolean): State<T> {
  return {
    key,
    entries: [],
    status: enabled ? 'loading' : 'ready',
    error: null,
    loadMoreError: null,
    nextCursor: null,
    loadingMore: false,
  };
}

function asApiError(cause: unknown): ApiError {
  return cause instanceof ApiError ? cause : new ApiError(0, 'Unexpected error.');
}

/**
 * The combined re-entrancy guard for `loadMore`. `loadingMore` alone is
 * not enough: it comes from React state, which only takes effect on the
 * next render, so two synchronous calls to `loadMore` — a double-tap, a
 * scroll handler firing twice — both see the same stale `false` and would
 * both start a request. `inFlight` is read from a ref instead, set the
 * instant the first call creates its controller, so the second call within
 * the same tick sees it and backs off.
 *
 * Doubles as a type guard on `nextCursor`: the caller's null check and the
 * re-entrancy check are one condition in practice (`loadMore` needs both
 * before it can fetch), so folding them into a single predicate narrows
 * `nextCursor` to `string` at the call site instead of checking null twice.
 */
export function canStartLoadMore(
  nextCursor: string | null,
  loadingMore: boolean,
  inFlight: boolean,
): nextCursor is string {
  return nextCursor !== null && !loadingMore && !inFlight;
}

function mergeUnique<T>(existing: T[], incoming: T[], idOf: (entry: T) => number): T[] {
  const seen = new Set(existing.map(idOf));
  const merged = [...existing];
  for (const entry of incoming) {
    const id = idOf(entry);
    if (seen.has(id)) continue;
    seen.add(id);
    merged.push(entry);
  }
  return merged;
}

/**
 * Cursor pagination against an opaque `next_cursor`. Pages accumulate;
 * every piece of state carries the cache key it belongs to, so a response
 * from a superseded filter set is discarded rather than rendered, and
 * merging by id keeps a duplicate across a cursor boundary from appearing
 * twice.
 *
 * The reset on a key change happens during render — React's documented
 * "adjust state when a prop changes" pattern — rather than in an effect,
 * so a filter change never paints the previous filter's items.
 */
export function usePagedResource<T>({
  key,
  enabled,
  idOf,
  fetchPage,
}: Options<T>): PagedResource<T> {
  const [reloadToken, setReloadToken] = useState(0);
  const cacheKey = `${String(reloadToken)}|${enabled ? '1' : '0'}|${key}`;

  const [state, setState] = useState<State<T>>(() => freshState<T>(cacheKey, enabled));
  if (state.key !== cacheKey) setState(freshState<T>(cacheKey, enabled));

  const fetchPageRef = useRef(fetchPage);
  const idOfRef = useRef(idOf);
  /**
   * The controller for whichever `loadMore` request is currently in
   * flight, or null when none is. Read synchronously by `loadMore`'s
   * re-entrancy guard — `state.loadingMore` only updates on the next
   * render, which is too late to stop a second synchronous call — and
   * aborted by the effect below on unmount or a cache-key change.
   */
  const loadMoreControllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    fetchPageRef.current = fetchPage;
    idOfRef.current = idOf;
  });

  useEffect(() => {
    if (!enabled) return undefined;
    const controller = new AbortController();
    fetchPageRef
      .current(undefined, controller.signal)
      .then((page) => {
        if (controller.signal.aborted) return;
        setState((current) =>
          current.key !== cacheKey
            ? current
            : {
                ...current,
                entries: page.entries,
                nextCursor: page.nextCursor,
                error: null,
                status: 'ready',
              },
        );
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setState((current) =>
          current.key !== cacheKey
            ? current
            : { ...current, error: asApiError(cause), status: 'error' },
        );
      });

    return () => {
      controller.abort();
    };
  }, [cacheKey, enabled]);

  // The one other request this hook can have open. Aborting it on the same
  // cleanup React already runs for the initial-page effect above — unmount,
  // or cacheKey changing underneath it — gives loadMore the lifecycle it
  // was missing: nothing is left running against a dead component, and a
  // filter change mid-scroll cannot leave a superseded request able to
  // write state once it resolves.
  useEffect(() => {
    return () => {
      loadMoreControllerRef.current?.abort();
      loadMoreControllerRef.current = null;
    };
  }, [cacheKey]);

  const { nextCursor, loadingMore } = state;

  const loadMore = useCallback(() => {
    if (!canStartLoadMore(nextCursor, loadingMore, loadMoreControllerRef.current !== null)) return;
    const controller = new AbortController();
    loadMoreControllerRef.current = controller;
    setState((current) =>
      current.key !== cacheKey ? current : { ...current, loadingMore: true, loadMoreError: null },
    );
    fetchPageRef
      .current(nextCursor, controller.signal)
      .then((page) => {
        if (loadMoreControllerRef.current === controller) loadMoreControllerRef.current = null;
        if (controller.signal.aborted) return;
        setState((current) =>
          current.key !== cacheKey
            ? current
            : {
                ...current,
                entries: mergeUnique(current.entries, page.entries, idOfRef.current),
                nextCursor: page.nextCursor,
                loadingMore: false,
              },
        );
      })
      .catch((cause: unknown) => {
        if (loadMoreControllerRef.current === controller) loadMoreControllerRef.current = null;
        if (controller.signal.aborted) return;
        setState((current) =>
          current.key !== cacheKey
            ? current
            : { ...current, loadMoreError: asApiError(cause), loadingMore: false },
        );
      });
  }, [cacheKey, nextCursor, loadingMore]);

  const reload = useCallback(() => {
    setReloadToken((value) => value + 1);
  }, []);

  // Guarded the same way every other setState in this hook is: these run
  // from optimistic mutation handlers, so the async call they wrap can
  // still be pending when the cache key moves on — a reload triggered by a
  // sibling failure, or a filter change the user made before the request
  // settled. Without the guard, a revert-on-failure closure captured
  // against the old generation would land on whatever the new generation's
  // entries happen to be, patching or removing an unrelated row that
  // reused the same id.
  const patchEntry = useCallback(
    (id: number, update: (entry: T) => T) => {
      setState((current) =>
        current.key !== cacheKey
          ? current
          : {
              ...current,
              entries: current.entries.map((entry) =>
                idOfRef.current(entry) === id ? update(entry) : entry,
              ),
            },
      );
    },
    [cacheKey],
  );

  const removeEntry = useCallback(
    (id: number) => {
      setState((current) =>
        current.key !== cacheKey
          ? current
          : { ...current, entries: current.entries.filter((entry) => idOfRef.current(entry) !== id) },
      );
    },
    [cacheKey],
  );

  return {
    entries: state.entries,
    status: state.status,
    error: state.error,
    loadMoreError: state.loadMoreError,
    hasMore: state.nextCursor !== null,
    loadingMore: state.loadingMore,
    loadMore,
    reload,
    patchEntry,
    removeEntry,
  };
}
