// @vitest-environment happy-dom
import { act, createElement, StrictMode, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client';
import { usePagedResource, type Page, type PagedResource } from './usePagedResource';

/**
 * The half of the hook `usePagedResource.test.ts` cannot reach: the effects.
 * Mounting it needs a DOM, so this file — and only this file, plus
 * `../api/client.test.ts` — opts into `happy-dom` through the docblock
 * above. The rest of the suite stays in vitest's `node` environment, where
 * a global the code did not install is a failure rather than a gift.
 *
 * There is no `@testing-library/react` here. React 19 exports `act` itself
 * and `createRoot` is public API, so the renderer below is thirty lines and
 * costs nothing transitively. It was written before the dependency was
 * argued for, rather than instead of arguing for it.
 *
 * Every `fetchPage` in these tests hands back a promise the test resolves by
 * hand. Timing is the whole subject — which response wins when two are open
 * — so nothing here is allowed to settle on its own schedule.
 */

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (cause: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (cause: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/**
 * Do `work`, then let React apply everything it queued. `act` flushes
 * effects; the explicit microtask turn inside it is what lets a `fetchPage`
 * promise resolved by `work` run its `.then` — and the `setState` in it —
 * before this returns. Every state transition in this file goes through
 * here, so nothing is ever asserted mid-flight by accident.
 */
async function settle(work: () => void = () => undefined): Promise<void> {
  await act(async () => {
    work();
    await Promise.resolve();
  });
}

// --- the renderer -------------------------------------------------------

interface Harness<P, R> {
  readonly current: R;
  rerender: (props: P) => Promise<void>;
  unmount: () => Promise<void>;
}

/**
 * Mount a hook in a component that renders nothing, and expose its latest
 * return value. That is the whole renderer: React 19 exports `act` and
 * `createRoot` is public API, so there is nothing here a library would do
 * differently.
 */
async function renderHook<P, R>(
  useHook: (props: P) => R,
  initialProps: P,
  wrap: (children: ReactNode) => ReactNode = (children) => children,
): Promise<Harness<P, R>> {
  const root = createRoot(document.createElement('div'));
  let latest: R | undefined;
  function Probe({ props }: { props: P }) {
    latest = useHook(props);
    return null;
  }

  const render = async (props: P) => {
    await settle(() => {
      root.render(wrap(createElement(Probe, { props })));
    });
  };

  await render(initialProps);
  return {
    get current(): R {
      return latest as R;
    },
    rerender: render,
    unmount: async () => {
      await settle(() => {
        root.unmount();
      });
    },
  };
}

// --- the resource under test -------------------------------------------

interface Row {
  id: number;
  title: string;
}

function rows(...ids: number[]): Row[] {
  return ids.map((id) => ({ id, title: `item ${String(id)}` }));
}

interface Call {
  cursor: string | undefined;
  signal: AbortSignal;
  pending: Deferred<Page<Row>>;
}

interface Props {
  key: string;
  enabled: boolean;
}

function scenario() {
  const calls: Call[] = [];
  const fetchPage = (cursor: string | undefined, signal: AbortSignal) => {
    const pending = deferred<Page<Row>>();
    calls.push({ cursor, signal, pending });
    return pending.promise;
  };
  const useSubject = ({ key, enabled }: Props): PagedResource<Row> =>
    usePagedResource<Row>({ key, enabled, idOf: (row) => row.id, fetchPage });
  return { calls, useSubject };
}

beforeEach(() => {
  // React 19 warns when `act` is used outside an environment that declares
  // itself one, and the warning is the only thing that would tell us the
  // renderer had stopped flushing.
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// --- the initial load ---------------------------------------------------

describe('the first page', () => {
  it('is requested on mount, with no cursor, and lands as ready', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });

    expect(hook.current.status).toBe('loading');
    expect(hook.current.entries).toEqual([]);
    expect(calls).toHaveLength(1);
    expect(calls[0].cursor).toBeUndefined();

    await settle(() => {
      calls[0].pending.resolve({ entries: rows(1, 2), nextCursor: 'c1' });
    });

    expect(hook.current.status).toBe('ready');
    expect(hook.current.entries).toEqual(rows(1, 2));
    expect(hook.current.hasMore).toBe(true);
    expect(hook.current.error).toBeNull();
  });

  it('reports no further pages when the cursor comes back null', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });
    await settle(() => {
      calls[0].pending.resolve({ entries: rows(1), nextCursor: null });
    });

    expect(hook.current.hasMore).toBe(false);
  });

  it('is not requested at all when the resource is disabled', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: false });

    // `useFeed` passes `enabled: false` when the user has deselected every
    // source: the answer is knowably empty, so asking is wasted work and a
    // spinner that never resolves into anything.
    expect(calls).toHaveLength(0);
    expect(hook.current.status).toBe('ready');
    expect(hook.current.entries).toEqual([]);
    expect(hook.current.error).toBeNull();
  });

  it('starts requesting once it becomes enabled', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: false });
    await hook.rerender({ key: 'a', enabled: true });

    expect(calls).toHaveLength(1);
    expect(hook.current.status).toBe('loading');
  });

  it('surfaces a failure as an error status, with the ApiError intact', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });

    await settle(() => {
      calls[0].pending.reject(new ApiError(503, 'Upstream is down.'));
    });

    expect(hook.current.status).toBe('error');
    expect(hook.current.error?.status).toBe(503);
    expect(hook.current.error?.message).toBe('Upstream is down.');
    expect(hook.current.entries).toEqual([]);
  });

  it('wraps a rejection that is not an ApiError rather than leaking it', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });

    await settle(() => {
      calls[0].pending.reject(new TypeError('cannot read properties of undefined'));
    });

    // The screens render `error.message` and branch on `error.status`;
    // handing them a bare TypeError breaks both.
    expect(hook.current.error).toBeInstanceOf(ApiError);
    expect(hook.current.error?.status).toBe(0);
    expect(hook.current.status).toBe('error');
  });
});

// --- pagination ---------------------------------------------------------

describe('loadMore', () => {
  it('carries the cursor from the previous page and appends the result', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });
    await settle(() => {
      calls[0].pending.resolve({ entries: rows(1, 2), nextCursor: 'c1' });
    });

    await settle(() => {
      hook.current.loadMore();
    });
    expect(calls).toHaveLength(2);
    expect(calls[1].cursor).toBe('c1');
    expect(hook.current.loadingMore).toBe(true);
    // The already-loaded page stays on screen while the next one is in
    // flight; this is not the full-page spinner.
    expect(hook.current.status).toBe('ready');
    expect(hook.current.entries).toEqual(rows(1, 2));

    await settle(() => {
      calls[1].pending.resolve({ entries: rows(3, 4), nextCursor: null });
    });

    expect(hook.current.entries).toEqual(rows(1, 2, 3, 4));
    expect(hook.current.loadingMore).toBe(false);
    expect(hook.current.hasMore).toBe(false);
  });

  it('does nothing when there is no next page', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });
    await settle(() => {
      calls[0].pending.resolve({ entries: rows(1), nextCursor: null });
    });

    await settle(() => {
      hook.current.loadMore();
    });

    expect(calls).toHaveLength(1);
  });

  it('starts one request for two calls in the same tick', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });
    await settle(() => {
      calls[0].pending.resolve({ entries: rows(1), nextCursor: 'c1' });
    });

    await settle(() => {
      // A double-tap on "Load more", or a scroll handler firing twice. No
      // render happens between them, so `loadingMore` is still false for
      // the second call and only the ref-backed guard stops it.
      hook.current.loadMore();
      hook.current.loadMore();
    });

    expect(calls).toHaveLength(2);
  });

  it('drops an entry that appears on both sides of a cursor boundary', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });
    await settle(() => {
      calls[0].pending.resolve({ entries: rows(1, 2), nextCursor: 'c1' });
    });
    await settle(() => {
      hook.current.loadMore();
    });
    await settle(() => {
      // An item ingested between the two requests shifts the window, so
      // the same row can be served twice. React would warn about the
      // duplicate key; the user would see the card twice.
      calls[1].pending.resolve({ entries: rows(2, 3), nextCursor: null });
    });

    expect(hook.current.entries).toEqual(rows(1, 2, 3));
  });

  it('keeps the loaded pages when a subsequent page fails', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });
    await settle(() => {
      calls[0].pending.resolve({ entries: rows(1, 2), nextCursor: 'c1' });
    });
    await settle(() => {
      hook.current.loadMore();
    });
    await settle(() => {
      calls[1].pending.reject(new ApiError(500, 'Server error.'));
    });

    // A failed second page is an inline retry, not an empty screen. The
    // separate `loadMoreError` field is what keeps `status` at ready.
    expect(hook.current.status).toBe('ready');
    expect(hook.current.error).toBeNull();
    expect(hook.current.loadMoreError?.status).toBe(500);
    expect(hook.current.entries).toEqual(rows(1, 2));
    expect(hook.current.loadingMore).toBe(false);
    expect(hook.current.hasMore).toBe(true);
  });

  it('clears the previous failure when the retry starts', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });
    await settle(() => {
      calls[0].pending.resolve({ entries: rows(1), nextCursor: 'c1' });
    });
    await settle(() => {
      hook.current.loadMore();
    });
    await settle(() => {
      calls[1].pending.reject(new ApiError(500, 'Server error.'));
    });

    await settle(() => {
      hook.current.loadMore();
    });

    expect(calls).toHaveLength(3);
    expect(hook.current.loadMoreError).toBeNull();
  });
});

// --- a filter change ----------------------------------------------------

describe('a change of key', () => {
  it('discards the loaded pages instead of appending to them', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });
    await settle(() => {
      calls[0].pending.resolve({ entries: rows(1, 2), nextCursor: 'c1' });
    });
    await settle(() => {
      hook.current.loadMore();
    });
    await settle(() => {
      calls[1].pending.resolve({ entries: rows(3, 4), nextCursor: 'c2' });
    });
    expect(hook.current.entries).toEqual(rows(1, 2, 3, 4));

    await hook.rerender({ key: 'b', enabled: true });

    // Emptied during the render that saw the new key, not in an effect
    // afterwards: a filter change must never paint the old filter's items.
    expect(hook.current.entries).toEqual([]);
    expect(hook.current.status).toBe('loading');
    expect(hook.current.hasMore).toBe(false);

    await settle(() => {
      calls[2].pending.resolve({ entries: rows(9), nextCursor: null });
    });

    expect(hook.current.entries).toEqual(rows(9));
  });

  it('forgets a previous page failure', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });
    await settle(() => {
      calls[0].pending.reject(new ApiError(500, 'Server error.'));
    });
    expect(hook.current.status).toBe('error');

    await hook.rerender({ key: 'b', enabled: true });

    expect(hook.current.status).toBe('loading');
    expect(hook.current.error).toBeNull();
  });

  it('aborts the request the previous key had open', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });
    expect(calls[0].signal.aborted).toBe(false);

    await hook.rerender({ key: 'b', enabled: true });

    // The signal reaches `fetch` through `endpoints.ts`, so this is a
    // cancelled connection, not just an ignored answer.
    expect(calls[0].signal.aborted).toBe(true);
  });

  it('aborts a loadMore that was still in flight', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });
    await settle(() => {
      calls[0].pending.resolve({ entries: rows(1), nextCursor: 'c1' });
    });
    await settle(() => {
      hook.current.loadMore();
    });
    expect(calls[1].signal.aborted).toBe(false);

    await hook.rerender({ key: 'b', enabled: true });

    expect(calls[1].signal.aborted).toBe(true);
  });
});

// --- the one that matters -----------------------------------------------

/**
 * Each of the hook's async continuations checks two things before it writes:
 * that its `AbortController` was not cancelled, and that the cache key it
 * captured is still the current one. Mutation testing says the pair is
 * load-bearing and neither half is, on its own — every transition that
 * supersedes a request performs both, so removing either alone is
 * unobservable here while removing both is caught below.
 *
 * That is not an argument for deleting one. The abort is what a
 * *cancellation* needs, and StrictMode produces a superseded response whose
 * cache key matches perfectly (the last case in this block). The cache-key
 * check is what a *stale closure* needs, and `patchEntry` has no request to
 * cancel. The overlap is the two mechanisms meeting in the middle, not
 * redundancy anyone should trim.
 */
describe('a response from a superseded request', () => {
  it('does not overwrite the newer key it arrived after', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });
    await hook.rerender({ key: 'b', enabled: true });

    await settle(() => {
      calls[1].pending.resolve({ entries: rows(9), nextCursor: null });
    });
    expect(hook.current.entries).toEqual(rows(9));

    // The slow request for the abandoned filter finally answers. The
    // classic version of this bug repaints the old filter's items over the
    // new ones, seconds after the user changed the filter, with nothing on
    // screen to explain it.
    await settle(() => {
      calls[0].pending.resolve({ entries: rows(1, 2), nextCursor: 'c-old' });
    });

    expect(hook.current.entries).toEqual(rows(9));
    expect(hook.current.hasMore).toBe(false);
  });

  it('does not turn the newer key into an error when the older one fails', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });
    await hook.rerender({ key: 'b', enabled: true });
    await settle(() => {
      calls[1].pending.resolve({ entries: rows(9), nextCursor: null });
    });

    await settle(() => {
      calls[0].pending.reject(new ApiError(500, 'Server error.'));
    });

    expect(hook.current.status).toBe('ready');
    expect(hook.current.error).toBeNull();
  });

  it('cannot append a superseded loadMore page to the new key', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });
    await settle(() => {
      calls[0].pending.resolve({ entries: rows(1), nextCursor: 'c1' });
    });
    await settle(() => {
      hook.current.loadMore();
    });

    await hook.rerender({ key: 'b', enabled: true });
    await settle(() => {
      calls[2].pending.resolve({ entries: rows(9), nextCursor: null });
    });

    await settle(() => {
      calls[1].pending.resolve({ entries: rows(2, 3), nextCursor: 'c2' });
    });

    expect(hook.current.entries).toEqual(rows(9));
    expect(hook.current.loadingMore).toBe(false);
    expect(hook.current.hasMore).toBe(false);
  });

  it('cannot report a superseded loadMore failure against the new key', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });
    await settle(() => {
      calls[0].pending.resolve({ entries: rows(1), nextCursor: 'c1' });
    });
    await settle(() => {
      hook.current.loadMore();
    });

    await hook.rerender({ key: 'b', enabled: true });
    await settle(() => {
      calls[2].pending.resolve({ entries: rows(9), nextCursor: 'c9' });
    });

    await settle(() => {
      calls[1].pending.reject(new ApiError(500, 'Server error.'));
    });

    // The abandoned filter's second page failing is not news about the
    // filter now on screen. Unguarded it would put a retry banner under a
    // list that loaded perfectly.
    expect(hook.current.loadMoreError).toBeNull();
    expect(hook.current.entries).toEqual(rows(9));
  });

  it('is discarded when the key it belongs to is the current one again', async () => {
    // The case the cache-key guard alone cannot catch, and the reason the
    // hook also checks `signal.aborted`. StrictMode mounts every effect,
    // tears it down, and mounts it again — twice in development, against
    // an unchanged key — so the first request's answer arrives with
    // `state.key` matching perfectly. Only the abort tells the two apart.
    // `main.tsx` wraps the whole app in StrictMode, so this is the path
    // every development page load actually takes.
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true }, (children) =>
      createElement(StrictMode, null, children),
    );

    expect(calls.length).toBeGreaterThanOrEqual(2);
    const [discarded, live] = calls;
    expect(discarded.signal.aborted).toBe(true);
    expect(live.signal.aborted).toBe(false);

    await settle(() => {
      live.pending.resolve({ entries: rows(9), nextCursor: null });
    });
    await settle(() => {
      discarded.pending.resolve({ entries: rows(1, 2), nextCursor: 'c-old' });
    });

    expect(hook.current.entries).toEqual(rows(9));
    expect(hook.current.hasMore).toBe(false);
  });
});

// --- reload -------------------------------------------------------------

describe('reload', () => {
  it('starts again from no cursor and clears the error', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });
    await settle(() => {
      calls[0].pending.reject(new ApiError(500, 'Server error.'));
    });

    await settle(() => {
      hook.current.reload();
    });

    expect(calls).toHaveLength(2);
    expect(calls[1].cursor).toBeUndefined();
    expect(hook.current.status).toBe('loading');
    expect(hook.current.error).toBeNull();
  });

  it('refetches even though the key has not changed', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });
    await settle(() => {
      calls[0].pending.resolve({ entries: rows(1, 2), nextCursor: 'c1' });
    });

    await settle(() => {
      hook.current.reload();
    });

    // The reload token is part of the cache key precisely so that this is
    // a new generation rather than a no-op re-run of the same effect.
    expect(calls).toHaveLength(2);
    expect(hook.current.entries).toEqual([]);
    expect(calls[0].signal.aborted).toBe(true);
  });
});

// --- optimistic mutation ------------------------------------------------

describe('patchEntry and removeEntry', () => {
  it('apply against whatever the list currently holds', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });
    await settle(() => {
      calls[0].pending.resolve({ entries: rows(1, 2, 3), nextCursor: null });
    });

    await settle(() => {
      hook.current.patchEntry(2, (row) => ({ ...row, title: 'read' }));
    });
    expect(hook.current.entries.map((row) => row.title)).toEqual(['item 1', 'read', 'item 3']);

    await settle(() => {
      hook.current.removeEntry(1);
    });
    expect(hook.current.entries.map((row) => row.id)).toEqual([2, 3]);
  });

  it('leave the list alone when the id is not in it', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });
    await settle(() => {
      calls[0].pending.resolve({ entries: rows(1), nextCursor: null });
    });
    const before = hook.current.entries;

    await settle(() => {
      hook.current.patchEntry(99, (row) => ({ ...row, title: 'nope' }));
      hook.current.removeEntry(99);
    });

    expect(hook.current.entries).toEqual(before);
  });

  it('become no-ops once the key has moved on', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });
    await settle(() => {
      calls[0].pending.resolve({ entries: rows(1, 2), nextCursor: null });
    });
    // Captured while filter "a" was on screen — a revert-on-failure
    // closure from an optimistic bookmark, say.
    const stalePatch = hook.current.patchEntry;
    const staleRemove = hook.current.removeEntry;

    await hook.rerender({ key: 'b', enabled: true });
    await settle(() => {
      calls[1].pending.resolve({ entries: rows(1, 2), nextCursor: null });
    });

    await settle(() => {
      stalePatch(1, (row) => ({ ...row, title: 'reverted' }));
      staleRemove(2);
    });

    // Same ids, different generation. Unguarded, the stale closures would
    // rewrite rows they have never seen. This is the guard the abort
    // cannot provide — there is no request to cancel.
    expect(hook.current.entries).toEqual(rows(1, 2));
  });
});

// --- unmount ------------------------------------------------------------

describe('unmount', () => {
  it('aborts everything it had open', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });
    await settle(() => {
      calls[0].pending.resolve({ entries: rows(1), nextCursor: 'c1' });
    });
    await settle(() => {
      hook.current.loadMore();
    });

    await hook.unmount();

    expect(calls[1].signal.aborted).toBe(true);
  });

  it('survives a response that arrives after it', async () => {
    const { calls, useSubject } = scenario();
    const hook = await renderHook(useSubject, { key: 'a', enabled: true });

    await hook.unmount();
    await settle(() => {
      calls[0].pending.resolve({ entries: rows(1), nextCursor: null });
    });
    await settle();

    expect(calls[0].signal.aborted).toBe(true);
  });
});
