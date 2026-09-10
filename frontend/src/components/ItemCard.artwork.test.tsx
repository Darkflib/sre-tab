// @vitest-environment happy-dom
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { FeedItem } from '../api/types';
import { ItemCard } from './ItemCard';

/**
 * The artwork chain: the item's own image, then the channel's, then
 * nothing.
 *
 * Worth mounting rather than reading, because the interesting behaviour is
 * what happens when a URL *fails* — which the markup cannot show and which
 * the previous single boolean got wrong by construction: one flag cannot
 * say which of two candidates broke, so a dead item image either took the
 * fallback down with it or was retried forever.
 */

const SOURCE = { slug: 'lobsters', name: 'Lobsters', icon_url: null as string | null };

function item(overrides: Partial<FeedItem> = {}): FeedItem {
  return {
    id: 1,
    canonical_url: 'https://example.org/one',
    title: 'One',
    summary: null,
    image_url: null,
    published_at: '2026-09-03T12:00:00Z',
    source: { ...SOURCE },
    topics: [],
    read: false,
    bookmarked: false,
    ...overrides,
  };
}

let host: HTMLDivElement;
let root: ReturnType<typeof createRoot>;

beforeEach(() => {
  host = document.createElement('div');
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  host.remove();
});

function render(entry: FeedItem): void {
  act(() => {
    root.render(
      createElement(ItemCard, {
        item: entry,
        onOpen: vi.fn(),
        onToggleRead: vi.fn(),
        onToggleBookmark: vi.fn(),
      }),
    );
  });
}

function image(): HTMLImageElement | null {
  return host.querySelector<HTMLImageElement>('.card__image');
}

function fail(): void {
  act(() => {
    image()?.dispatchEvent(new Event('error'));
  });
}

describe('ItemCard artwork', () => {
  it('shows the item image when it has one', () => {
    render(item({ image_url: 'https://cdn/story.jpg' }));

    expect(image()?.getAttribute('src')).toBe('https://cdn/story.jpg');
    expect(image()?.className).not.toContain('card__image--channel');
  });

  it('falls back to the channel image when the item has none', () => {
    // The case this whole change exists for: Hacker News, Lobsters and LWN
    // publish an item image about never.
    render(item({ source: { ...SOURCE, icon_url: 'https://cdn/logo.png' } }));

    expect(image()?.getAttribute('src')).toBe('https://cdn/logo.png');
  });

  it('styles the fallback as a mark rather than as a photograph', () => {
    render(item({ source: { ...SOURCE, icon_url: 'https://cdn/logo.png' } }));

    expect(image()?.className).toContain('card__image--channel');
  });

  it('shows nothing when neither exists', () => {
    render(item());

    expect(image()).toBeNull();
  });

  it('falls through to the channel image when the item image fails to load', () => {
    render(
      item({ image_url: 'https://cdn/broken.jpg', source: { ...SOURCE, icon_url: 'https://cdn/logo.png' } }),
    );
    expect(image()?.getAttribute('src')).toBe('https://cdn/broken.jpg');

    fail();

    expect(image()?.getAttribute('src')).toBe('https://cdn/logo.png');
    expect(image()?.className).toContain('card__image--channel');
  });

  it('gives up rather than retrying when the channel image fails too', () => {
    // The regression a single boolean would produce: the chain re-offers
    // the URL that just failed, the browser fires `error` again, and the
    // card loops.
    render(
      item({ image_url: 'https://cdn/broken.jpg', source: { ...SOURCE, icon_url: 'https://cdn/gone.png' } }),
    );

    fail();
    fail();

    expect(image()).toBeNull();
  });

  it('gives up when the only candidate fails', () => {
    render(item({ source: { ...SOURCE, icon_url: 'https://cdn/gone.png' } }));

    fail();

    expect(image()).toBeNull();
  });

  it('never sends a referrer for either', () => {
    // Both URLs are third-party and the reader did not ask for them; the
    // page they are on is not the publisher's business.
    render(item({ source: { ...SOURCE, icon_url: 'https://cdn/logo.png' } }));

    expect(image()?.getAttribute('referrerpolicy')).toBe('no-referrer');
  });
});
