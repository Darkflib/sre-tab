// @vitest-environment happy-dom
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SearchBox, SEARCH_DEBOUNCE_MS } from './SearchBox';

/**
 * The search box, mounted for real, because everything worth checking here
 * is timing and none of it is visible in the markup.
 *
 * The renderer is hand-rolled over `createRoot` and React 19's own `act`,
 * following `FilterBar.collapse.test.tsx` — a dependency is a supply-chain
 * decision and a renderer costs thirty lines.
 *
 * Three behaviours, and each of them is a bug this component would have
 * without it: a request per keystroke, an input that keeps showing a search
 * the feed is no longer running, and a cursor that jumps to the end of the
 * field mid-word because the box echoed its own commit back to itself.
 */

let host: HTMLDivElement;
let root: ReturnType<typeof createRoot>;

beforeEach(() => {
  vi.useFakeTimers();
  host = document.createElement('div');
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  host.remove();
  vi.useRealTimers();
});

function render(value: string, onChange: (next: string) => void): void {
  act(() => {
    root.render(createElement(SearchBox, { value, onChange }));
  });
}

function input(): HTMLInputElement {
  const field = host.querySelector<HTMLInputElement>('.search__input');
  if (!field) throw new Error('no search input rendered');
  return field;
}

/**
 * React tracks an input's value behind the element's own setter and drops a
 * change it believes it already has, so assigning `field.value` directly
 * dispatches an event React ignores. Going through the prototype descriptor
 * is what makes the change visible to it — the same helper
 * `../routes/ApiTokensSection.test.tsx` needs, for the same reason.
 */
function type(text: string): void {
  const descriptor = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
  if (!descriptor?.set) throw new Error('HTMLInputElement has no value setter');
  act(() => {
    const field = input();
    descriptor.set?.call(field, text);
    field.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

function press(key: string): void {
  act(() => {
    input().dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));
  });
}

function settle(ms = SEARCH_DEBOUNCE_MS): void {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
}

describe('SearchBox', () => {
  it('commits once after the reader stops typing, not once per keystroke', () => {
    // Each commit is a new `filterKey`, which discards every loaded page
    // and refetches from the top. Per keystroke that is eight requests and
    // eight thrown-away feeds to type one word.
    const onChange = vi.fn();
    render('', onChange);

    for (const partial of ['p', 'po', 'pos', 'post']) {
      type(partial);
      settle(SEARCH_DEBOUNCE_MS - 50);
    }
    expect(onChange).not.toHaveBeenCalled();

    settle();
    expect(onChange.mock.calls).toEqual([['post']]);
  });

  it('does not commit a value it was given', () => {
    // The committed value arriving back as a prop is the normal case — the
    // URL updates and the parent re-renders. Treating it as a change would
    // be an unbounded loop.
    const onChange = vi.fn();
    render('postgres', onChange);
    settle();

    expect(onChange).not.toHaveBeenCalled();
  });

  it('shows a query cleared from outside it', () => {
    // "Clear filters" and the back button both move the query without
    // touching this box. Without the resynchronising effect the input goes
    // on displaying a search the feed has stopped running.
    const onChange = vi.fn();
    render('postgres', onChange);
    expect(input().value).toBe('postgres');

    render('', onChange);
    expect(input().value).toBe('');
  });

  it('does not reset what the reader is typing when its own commit returns', () => {
    // The regression this guards: a reader who keeps typing through the
    // debounce has the input replaced by the shorter, already-committed
    // value, which in a real browser also throws the caret to the end.
    const onChange = vi.fn();
    render('', onChange);

    type('post');
    settle();
    expect(onChange.mock.calls).toEqual([['post']]);

    type('postgres');
    render('post', onChange); // the commit above, arriving back as a prop

    expect(input().value).toBe('postgres');
  });

  it('commits immediately on Enter rather than waiting out the debounce', () => {
    const onChange = vi.fn();
    render('', onChange);

    type('  postgres  ');
    press('Enter');

    expect(onChange.mock.calls).toEqual([['postgres']]);
  });

  it('clears on Escape', () => {
    const onChange = vi.fn();
    render('postgres', onChange);

    type('postgres');
    press('Escape');

    expect(onChange.mock.calls).toEqual([['']]);
    expect(input().value).toBe('');
  });

  it('offers a clear button only when there is something to clear', () => {
    const onChange = vi.fn();
    render('', onChange);
    expect(host.querySelector('.search__clear')).toBeNull();

    type('postgres');
    expect(host.querySelector('.search__clear')).not.toBeNull();

    act(() => {
      host.querySelector<HTMLButtonElement>('.search__clear')?.click();
    });
    expect(onChange.mock.calls).toEqual([['']]);
  });

  it('stops at the length the server accepts', () => {
    // 200 is `FEED_MAX_QUERY_LENGTH`; past it the API answers 422, which
    // would be an error banner over the whole feed rather than a box that
    // simply stops taking characters.
    render('', vi.fn());

    expect(input().maxLength).toBe(200);
  });
});
