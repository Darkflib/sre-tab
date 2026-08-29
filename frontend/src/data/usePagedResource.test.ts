import { describe, expect, it } from 'vitest';

import { canStartLoadMore } from './usePagedResource';

/**
 * `usePagedResource` needs a renderer to test properly — `loadMore` closes
 * over `useState`/`useRef`/`useCallback`, and exercising its actual
 * lifecycle (an effect's cleanup firing on unmount, a re-render replacing a
 * stale closure) means mounting a component. This project's suite runs
 * without jsdom or a testing-library renderer by deliberate choice, and
 * neither is being added for one hook.
 *
 * What *is* pure, and what the lifecycle fix actually depends on, is the
 * re-entrancy guard `loadMore` checks before it is allowed to start a
 * second request. `canStartLoadMore` is that guard extracted to a plain
 * function, so the one piece of new decision logic in the fix is checked
 * here directly rather than asserted only by inspection.
 *
 * The guard exists because `loadingMore` alone cannot stop a same-tick
 * double call: it is React state, so it only reflects the first call once
 * a render has happened, and nothing forces a render between two
 * synchronous invocations (a double-tap, or a scroll handler firing
 * twice). The fix adds a second, ref-backed signal — `inFlight` — that is
 * true the instant the first call creates its `AbortController`, before
 * any render. These cases exist to pin exactly that: `loadingMore=false`
 * must not be enough on its own once `inFlight=true`.
 */
describe('canStartLoadMore', () => {
  it('allows a request when there is a next page and nothing in flight', () => {
    expect(canStartLoadMore('cursor-1', false, false)).toBe(true);
  });

  it('refuses when there is no next page', () => {
    // hasMore is false; nothing to load regardless of the other two.
    expect(canStartLoadMore(null, false, false)).toBe(false);
  });

  it('refuses when React state already says a load is in progress', () => {
    expect(canStartLoadMore('cursor-1', true, false)).toBe(false);
  });

  it('refuses when a request is in flight even though state has not caught up yet', () => {
    // This is the case state alone gets wrong: loadingMore is still false
    // because no render has happened since the first call, but the ref
    // already has a controller. A guard that only checked loadingMore
    // would let a second request start here.
    expect(canStartLoadMore('cursor-1', false, true)).toBe(false);
  });

  it('refuses when both signals say a load is already happening', () => {
    expect(canStartLoadMore('cursor-1', true, true)).toBe(false);
  });

  it.each([
    ['no next page and nothing in flight', null, false, false],
    ['no next page but state stuck loading', null, true, false],
    ['no next page and something in flight', null, false, true],
  ] as const)('%s stays refused (cursor guard is not overridden by the others)', (_label, cursor, loadingMore, inFlight) => {
    expect(canStartLoadMore(cursor, loadingMore, inFlight)).toBe(false);
  });
});
