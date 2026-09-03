import { afterEach, describe, expect, it, vi } from 'vitest';

import type { FeedFilters } from './filters';
import {
  FILTERS_COLLAPSED_STORAGE_KEY,
  type NamedEntry,
  readFiltersCollapsed,
  rememberFiltersCollapsed,
  summariseFilters,
} from './collapse';

/**
 * vitest's default `node` environment, like `theme.test.ts` and for the same
 * reason: every global this module touches is installed by hand below, so a
 * new dependency on one shows up as a failure rather than being supplied
 * silently by an ambient DOM. The storage cases in particular are about what
 * happens when `window.localStorage` is *not* an ordinary Storage, which a
 * real DOM would helpfully hide.
 */

interface StorageStub {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

function installWindow(storage?: StorageStub) {
  vi.stubGlobal('window', { localStorage: storage });
}

function memoryStorage(initial: Record<string, string> = {}): StorageStub {
  const map = new Map(Object.entries(initial));
  return {
    getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => {
      map.set(key, value);
    },
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

// --- the stored flag ----------------------------------------------------

describe('readFiltersCollapsed', () => {
  it('is expanded when nothing has been stored', () => {
    // The default nobody chose. A first visit must look exactly like it did
    // before this existed.
    installWindow(memoryStorage());
    expect(readFiltersCollapsed()).toBe(false);
  });

  it('is collapsed for the stored collapsed value', () => {
    installWindow(memoryStorage({ [FILTERS_COLLAPSED_STORAGE_KEY]: 'collapsed' }));
    expect(readFiltersCollapsed()).toBe(true);
  });

  it('is expanded for the stored expanded value', () => {
    installWindow(memoryStorage({ [FILTERS_COLLAPSED_STORAGE_KEY]: 'expanded' }));
    expect(readFiltersCollapsed()).toBe(false);
  });

  it('is expanded for a value we did not write', () => {
    // A stale key from an older build, or another tab writing nonsense.
    installWindow(memoryStorage({ [FILTERS_COLLAPSED_STORAGE_KEY]: 'true' }));
    expect(readFiltersCollapsed()).toBe(false);
  });

  it('reads the key it writes and no other', () => {
    installWindow(memoryStorage({ 'dnd.theme': 'collapsed' }));
    expect(readFiltersCollapsed()).toBe(false);
  });

  it('is expanded when storage throws', () => {
    // Safari in private mode, or a blocked-cookies setting.
    installWindow({
      getItem: () => {
        throw new Error('SecurityError');
      },
      setItem: () => undefined,
    });
    expect(readFiltersCollapsed()).toBe(false);
  });

  it('is expanded when there is no storage at all', () => {
    installWindow();
    expect(readFiltersCollapsed()).toBe(false);
  });

  it('is expanded when there is no window at all', () => {
    // Not reachable in the browser build, and the assertion is that this
    // degrades rather than throwing: nothing about a filter bar justifies
    // taking a page down.
    vi.stubGlobal('window', undefined);
    expect(readFiltersCollapsed()).toBe(false);
  });
});

describe('rememberFiltersCollapsed', () => {
  it('stores each state under the shared key', () => {
    const storage = memoryStorage();
    installWindow(storage);

    rememberFiltersCollapsed(true);
    expect(storage.getItem(FILTERS_COLLAPSED_STORAGE_KEY)).toBe('collapsed');

    rememberFiltersCollapsed(false);
    expect(storage.getItem(FILTERS_COLLAPSED_STORAGE_KEY)).toBe('expanded');
  });

  it('round-trips through the reader in both directions', () => {
    // Writing "collapsed" and never writing anything else would satisfy the
    // test above and leave a reader unable to expand the bar again.
    installWindow(memoryStorage());

    rememberFiltersCollapsed(true);
    expect(readFiltersCollapsed()).toBe(true);

    rememberFiltersCollapsed(false);
    expect(readFiltersCollapsed()).toBe(false);
  });

  it('never throws when storage is unavailable', () => {
    installWindow({
      getItem: () => null,
      setItem: () => {
        throw new Error('QuotaExceededError');
      },
    });
    expect(() => {
      rememberFiltersCollapsed(true);
    }).not.toThrow();
  });
});

// --- the collapsed summary ----------------------------------------------

const SOURCES: NamedEntry[] = [
  { slug: 'lwn', name: 'LWN' },
  { slug: 'hn', name: 'Hacker News' },
  { slug: 'phoronix', name: 'Phoronix' },
  { slug: 'lobsters', name: 'Lobsters' },
  { slug: 'ars', name: 'Ars Technica' },
];

const TOPICS: NamedEntry[] = [
  { slug: 'kernel', name: 'Kernel' },
  { slug: 'security', name: 'Security' },
];

const CATALOGUE = { sources: SOURCES, topics: TOPICS };

function filters(overrides: Partial<FeedFilters> = {}): FeedFilters {
  return { topics: null, sources: null, readState: 'all', query: '', ...overrides };
}

describe('summariseFilters', () => {
  it('says nothing at all when no dimension is overridden', () => {
    // The badge already says "Your selection"; a phrase here would tell a
    // reader they are filtering when they are not.
    expect(summariseFilters(filters(), CATALOGUE)).toEqual([]);
  });

  it('keeps no override and an empty selection apart', () => {
    // One character apart in the source and opposite in meaning: `null`
    // widens to the saved selection, `[]` narrows to nothing.
    expect(summariseFilters(filters({ sources: null }), CATALOGUE)).toEqual([]);
    expect(summariseFilters(filters({ sources: [] }), CATALOGUE)).toEqual([
      'No sources selected',
    ]);
  });

  it('names an empty topic selection separately from an empty source one', () => {
    expect(summariseFilters(filters({ topics: [] }), CATALOGUE)).toEqual(['No topics selected']);
  });

  it('names a single selection', () => {
    expect(summariseFilters(filters({ sources: ['hn'] }), CATALOGUE)).toEqual([
      'Sources: Hacker News',
    ]);
  });

  it('joins two with and, and no comma', () => {
    expect(summariseFilters(filters({ sources: ['lwn', 'hn'] }), CATALOGUE)).toEqual([
      'Sources: LWN and Hacker News',
    ]);
  });

  it('keeps the Oxford comma at three', () => {
    expect(summariseFilters(filters({ sources: ['lwn', 'hn', 'phoronix'] }), CATALOGUE)).toEqual([
      'Sources: LWN, Hacker News, and Phoronix',
    ]);
  });

  it('counts the remainder past three rather than listing everything', () => {
    expect(
      summariseFilters(filters({ sources: ['lwn', 'hn', 'phoronix', 'lobsters'] }), CATALOGUE),
    ).toEqual(['Sources: LWN, Hacker News, Phoronix, and 1 more']);

    expect(
      summariseFilters(
        filters({ sources: ['lwn', 'hn', 'phoronix', 'lobsters', 'ars'] }),
        CATALOGUE,
      ),
    ).toEqual(['Sources: LWN, Hacker News, Phoronix, and 2 more']);
  });

  it('preserves the selection order rather than the catalogue order', () => {
    expect(summariseFilters(filters({ sources: ['hn', 'lwn'] }), CATALOGUE)).toEqual([
      'Sources: Hacker News and LWN',
    ]);
  });

  it('falls back to the slug for a source the catalogue no longer carries', () => {
    // A shared link, or a source disabled since. It is still narrowing the
    // feed, so dropping it would shorten the list and misreport the filter.
    expect(summariseFilters(filters({ sources: ['hn', 'retired'] }), CATALOGUE)).toEqual([
      'Sources: Hacker News and retired',
    ]);
  });

  it('is still an override when the selection happens to be everything', () => {
    // `["a","b","c"]` for a three-source catalogue selects the same items as
    // `null`, and it is not the same state: it is pinned, so a source added
    // tomorrow will not appear. Summarising it as "no filters" would hide
    // exactly that.
    const everything = SOURCES.map((entry) => entry.slug);
    expect(summariseFilters(filters({ sources: everything }), CATALOGUE)).toEqual([
      'Sources: LWN, Hacker News, Phoronix, and 2 more',
    ]);
  });

  it('names a read-state narrowing, in both directions', () => {
    expect(summariseFilters(filters({ readState: 'unread', query: '' }), CATALOGUE)).toEqual(['Unread only']);
    expect(summariseFilters(filters({ readState: 'read', query: '' }), CATALOGUE)).toEqual(['Read only']);
  });

  it('says nothing about read state when it is not narrowing', () => {
    expect(summariseFilters(filters({ readState: 'all', query: '' }), CATALOGUE)).toEqual([]);
  });

  it('reports every narrowed dimension, sources then topics then read state', () => {
    expect(
      summariseFilters(
        filters({ sources: ['hn'], topics: ['kernel'], readState: 'unread', query: '' }),
        CATALOGUE,
      ),
    ).toEqual(['Sources: Hacker News', 'Topics: Kernel', 'Unread only']);
  });

  it('reports one dimension without inventing the other', () => {
    expect(summariseFilters(filters({ topics: ['security'] }), CATALOGUE)).toEqual([
      'Topics: Security',
    ]);
  });

  it('does not mutate the selection it is given', () => {
    const sources = ['phoronix', 'hn'];
    summariseFilters(filters({ sources }), CATALOGUE);
    expect(sources).toEqual(['phoronix', 'hn']);
  });

  it('survives an empty catalogue by naming the slugs', () => {
    expect(
      summariseFilters(filters({ sources: ['hn'] }), { sources: [], topics: [] }),
    ).toEqual(['Sources: hn']);
  });
});
