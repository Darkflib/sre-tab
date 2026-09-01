import { describe, expect, it } from 'vitest';

import type { Preferences, Source, Topic } from '../api/types';
import {
  applyFiltersToParams,
  effectiveSelection,
  EMPTY_FILTERS,
  type FeedFilters,
  filterKey,
  hasOverride,
  hasSavableOverride,
  parseFilters,
  selectsNothing,
  toggle,
} from './filters';

/**
 * Every function here turns on one distinction: `null` means "no override,
 * use my saved selection" and `[]` means "the user deselected everything,
 * so nothing can match". They are one character apart in the source and
 * they mean opposite things — `null` widens to the saved selection, `[]`
 * narrows to nothing — and the server agrees:
 * `app/services/feed.py:_effective_sources` honours an explicit request
 * verbatim, so `[]` returns an empty page, while an absent parameter falls
 * back to the saved selection and an *empty saved selection* means the
 * instance defaults, which is everything.
 *
 * That is three states behind two representations, and the failure mode is
 * silent in both directions: collapse `[]` to `null` and a user who asked
 * for nothing gets the whole catalogue; promote `null` to `[]` and a user
 * who asked for nothing in particular gets an empty feed. Neither throws.
 *
 * These run in vitest's default `node` environment. `URLSearchParams` is a
 * standard global there, and the module imports types only, so nothing is
 * stubbed.
 */

// --- fixtures -----------------------------------------------------------

function source(slug: string, refreshMinutes = 60): Source {
  return {
    slug,
    name: slug.toUpperCase(),
    feed_url: `https://example.invalid/${slug}.xml`,
    website_url: `https://example.invalid/${slug}`,
    icon_url: null,
    refresh_minutes: refreshMinutes,
    enabled: true,
    topics: [],
  };
}

function topic(slug: string): Topic {
  return { slug, name: slug, enabled: true };
}

function preferences(overrides: Partial<Preferences> = {}): Preferences {
  return {
    theme: 'system',
    layout: 'grid',
    max_visible_cards: 50,
    onboarding_completed: true,
    topics: [],
    sources: [],
    ...overrides,
  };
}

const CATALOGUE = {
  sources: [source('lwn'), source('hn', 15), source('phoronix')],
  topics: [topic('kernel'), topic('security')],
};

// --- parseFilters -------------------------------------------------------

describe('parseFilters', () => {
  it('reads an absent parameter as no override', () => {
    expect(parseFilters(new URLSearchParams(''))).toEqual(EMPTY_FILTERS);
  });

  it('reads a present but empty parameter as an empty selection', () => {
    // `?topics=` is the URL a "None" click produces. It must not read back
    // as an absent parameter, or the feed silently widens to the saved
    // selection on the next page load.
    expect(parseFilters(new URLSearchParams('topics='))).toEqual({
      topics: [],
      sources: null,
      readState: 'all',
    });
  });

  it('reads the two dimensions independently', () => {
    expect(parseFilters(new URLSearchParams('topics=kernel'))).toEqual({
      topics: ['kernel'],
      sources: null,
      readState: 'all',
    });
    expect(parseFilters(new URLSearchParams('sources=lwn'))).toEqual({
      topics: null,
      sources: ['lwn'],
      readState: 'all',
    });
  });

  it('splits on commas, trims, and drops blanks', () => {
    expect(parseFilters(new URLSearchParams('topics=%20kernel%20,,%20security%20'))).toEqual({
      topics: ['kernel', 'security'],
      sources: null,
      readState: 'all',
    });
  });

  it('treats a parameter of only separators as an empty selection', () => {
    // Every entry is dropped as blank, leaving `[]` — which is "nothing
    // selected", not "no override". A hand-edited URL reaches this.
    expect(parseFilters(new URLSearchParams('sources=,,,'))).toEqual({
      topics: null,
      sources: [],
      readState: 'all',
    });
  });

  it('reads an absent read_state as all', () => {
    // The default the server also applies. This and the `all` case below
    // are what keep an old bookmarked URL showing the same feed it did.
    expect(parseFilters(new URLSearchParams('')).readState).toBe('all');
  });

  it.each(['unread', 'read', 'all'] as const)('reads read_state=%s', (value) => {
    expect(parseFilters(new URLSearchParams(`read_state=${value}`)).readState).toBe(value);
  });

  it.each(['', 'true', 'false', 'UNREAD', 'nonsense', 'unread,read'])(
    'reads an unrecognised read_state of %o as all',
    (value) => {
      // Not passed through to the server, which answers a value outside
      // the enum with a 422: a typo in a hand-edited URL should widen to
      // the default feed, not replace the page with an error banner.
      expect(parseFilters(new URLSearchParams(`read_state=${value}`)).readState).toBe('all');
    },
  );

  it('reads read_state independently of the two array dimensions', () => {
    expect(parseFilters(new URLSearchParams('topics=kernel&read_state=unread'))).toEqual({
      topics: ['kernel'],
      sources: null,
      readState: 'unread',
    });
  });

  it('keeps duplicates rather than collapsing them', () => {
    // No de-duplication happens here; `filterKey` and the server both cope,
    // and silently rewriting what the URL said would be the surprising
    // behaviour.
    expect(parseFilters(new URLSearchParams('sources=lwn,lwn')).sources).toEqual(['lwn', 'lwn']);
  });
});

// --- applyFiltersToParams -----------------------------------------------

describe('applyFiltersToParams', () => {
  it('deletes the parameter for no override', () => {
    const next = applyFiltersToParams(new URLSearchParams('topics=kernel&sources=lwn'), EMPTY_FILTERS);
    expect(next.has('topics')).toBe(false);
    expect(next.has('sources')).toBe(false);
  });

  it('writes an empty value for an empty selection', () => {
    // Not a deletion: the parameter has to survive as `topics=` so that the
    // reload reads `[]` back.
    const next = applyFiltersToParams(new URLSearchParams(''), {
      topics: [],
      sources: null,
      readState: 'all',
    });
    expect(next.get('topics')).toBe('');
    expect(next.toString()).toBe('topics=');
  });

  it('preserves unrelated parameters', () => {
    const next = applyFiltersToParams(new URLSearchParams('view=compact'), {
      topics: ['kernel'],
      sources: null,
      readState: 'all',
    });
    expect(next.get('view')).toBe('compact');
    expect(next.get('topics')).toBe('kernel');
  });

  it('does not mutate the parameters it is given', () => {
    const original = new URLSearchParams('topics=kernel');
    applyFiltersToParams(original, { topics: [], sources: ['lwn'], readState: 'all' });
    expect(original.toString()).toBe('topics=kernel');
  });

  it('writes read_state only when it narrows something', () => {
    // `all` is the default on both sides, so writing it would put a
    // parameter that says nothing into every shared link.
    const filtered = applyFiltersToParams(new URLSearchParams(''), {
      ...EMPTY_FILTERS,
      readState: 'unread',
    });
    expect(filtered.toString()).toBe('read_state=unread');
    expect(applyFiltersToParams(new URLSearchParams(''), EMPTY_FILTERS).has('read_state')).toBe(
      false,
    );
  });

  it('clears a read_state already in the URL when it goes back to all', () => {
    const next = applyFiltersToParams(new URLSearchParams('read_state=read'), EMPTY_FILTERS);
    expect(next.has('read_state')).toBe(false);
  });
});

// --- the round trip -----------------------------------------------------

const ROUND_TRIPS: [string, FeedFilters][] = [
  ['no override at all', { topics: null, sources: null, readState: 'all' }],
  ['a topic override only', { topics: ['kernel'], sources: null, readState: 'all' }],
  ['a source override only', { topics: null, sources: ['lwn'], readState: 'all' }],
  [
    'both dimensions overridden',
    { topics: ['kernel', 'security'], sources: ['lwn', 'hn'], readState: 'all' },
  ],
  ['everything deselected', { topics: [], sources: [], readState: 'all' }],
  ['one dimension deselected, the other untouched', { topics: [], sources: null, readState: 'all' }],
  ['one dimension deselected, the other narrowed', { topics: [], sources: ['lwn'], readState: 'all' }],
  ['unread only', { topics: null, sources: null, readState: 'unread' }],
  ['read only', { topics: null, sources: null, readState: 'read' }],
  ['unread only within a narrowed source', { topics: null, sources: ['lwn'], readState: 'unread' }],
  ['all three dimensions at once', { topics: ['kernel'], sources: ['lwn'], readState: 'read' }],
  ['read state set while everything is deselected', { topics: [], sources: [], readState: 'read' }],
];

describe('the URL round trip', () => {
  // The URL is where this state lives between renders — FeedPage derives
  // `filters` from `searchParams` on every pass — so a filter that does not
  // survive serialisation is a filter that resets itself.
  it.each(ROUND_TRIPS)('survives %s', (_label, filters) => {
    const params = applyFiltersToParams(new URLSearchParams(''), filters);
    expect(parseFilters(params)).toEqual(filters);
  });

  it('is not confused by a stale parameter already in the URL', () => {
    const params = applyFiltersToParams(new URLSearchParams('topics=security'), {
      topics: null,
      sources: [],
      readState: 'all',
    });
    expect(parseFilters(params)).toEqual({ topics: null, sources: [], readState: 'all' });
  });

  // Marked `.fails`: this asserts the behaviour we want and records that
  // the client does not have it. A slug containing a comma is split in two
  // by the round trip, because the comma is the separator and nothing
  // escapes it.
  //
  // It is *contained* rather than fixed. `add_source` and `add_topic` now
  // refuse a slug that is not lower-case alphanumerics with single
  // hyphens, so no such slug can be created, and `sre-tab status` reports
  // any that predate the check. That is enforcement at the point the
  // operator chooses the value rather than tolerance three components
  // downstream — but it is a constraint held elsewhere, so the client
  // stays fragile to a slug it will now never see.
  //
  // Asserting the current behaviour instead would pin the defect as
  // correct and fail whoever hardens the client. `.fails` inverts that:
  // the day serialisation preserves arbitrary slugs, this errors with
  // "expected to fail but passed" and whoever did it deletes the marker.
  it.fails('preserves a slug containing a comma', () => {
    const params = applyFiltersToParams(new URLSearchParams(''), {
      topics: null,
      sources: ['a,b'],
      readState: 'all',
    });
    expect(parseFilters(params).sources).toEqual(['a,b']);
  });
});

// --- hasOverride / selectsNothing ---------------------------------------

const OVERRIDE_CASES: [string, FeedFilters, boolean][] = [
  ['neither dimension set', { topics: null, sources: null, readState: 'all' }, false],
  ['topics narrowed', { topics: ['kernel'], sources: null, readState: 'all' }, true],
  ['sources narrowed', { topics: null, sources: ['lwn'], readState: 'all' }, true],
  ['topics deselected entirely', { topics: [], sources: null, readState: 'all' }, true],
  ['both set', { topics: [], sources: ['lwn'], readState: 'all' }, true],
  // Read state counts as an override on its own: it is the only thing
  // putting "Clear filters" on screen, and a user who narrowed to unread
  // and saw no badge would have a short feed and no way back.
  ['read state narrowed on its own', { topics: null, sources: null, readState: 'unread' }, true],
  ['read state narrowed to read', { topics: null, sources: null, readState: 'read' }, true],
  [
    'read state alongside a narrowed source',
    { topics: null, sources: ['lwn'], readState: 'read' },
    true,
  ],
];

describe('hasOverride', () => {
  // This drives the "Filtered" badge and the Clear/Save controls, so an
  // empty selection has to count as an override — otherwise deselecting
  // everything offers the user no way back.
  it.each(OVERRIDE_CASES)('%s', (_label, filters, expected) => {
    expect(hasOverride(filters)).toBe(expected);
  });
});

const NOTHING_CASES: [string, FeedFilters, boolean][] = [
  ['no override', { topics: null, sources: null, readState: 'all' }, false],
  ['both narrowed to something', { topics: ['kernel'], sources: ['lwn'], readState: 'all' }, false],
  ['topics deselected', { topics: [], sources: null, readState: 'all' }, true],
  ['sources deselected', { topics: null, sources: [], readState: 'all' }, true],
  [
    'sources deselected while topics are narrowed',
    { topics: ['kernel'], sources: [], readState: 'all' },
    true,
  ],
  ['both deselected', { topics: [], sources: [], readState: 'all' }, true],
  // Read state is deliberately absent from this predicate. "Unread only"
  // can return nothing, but whether it does is a fact about the database,
  // and claiming it here would render an empty state over a feed that has
  // items — and skip the request that would have proved otherwise.
  ['unread only, nothing else set', { topics: null, sources: null, readState: 'unread' }, false],
  ['read only, nothing else set', { topics: null, sources: null, readState: 'read' }, false],
  ['unread only with sources deselected', { topics: null, sources: [], readState: 'unread' }, true],
];

describe('selectsNothing', () => {
  // `useFeed` turns this straight into `enabled`, so a false negative asks
  // the server a question with no answer and a false positive renders an
  // empty state over a feed that has items.
  it.each(NOTHING_CASES)('%s', (_label, filters, expected) => {
    expect(selectsNothing(filters)).toBe(expected);
  });

  it('is false for a null selection rather than undefined-comparing to zero', () => {
    // `filters.topics?.length === 0` is `undefined === 0` when topics is
    // null. Pinned because rewriting it as `!filters.topics?.length` would
    // invert the null case and look like a tidy-up.
    expect(selectsNothing(EMPTY_FILTERS)).toBe(false);
  });
});

// --- filterKey ----------------------------------------------------------

describe('filterKey', () => {
  it('is stable under selection order', () => {
    // The cache key has to be identity, not sequence: toggling a chip off
    // and on again reorders the array and must not refetch.
    expect(filterKey({ topics: ['a', 'b'], sources: null, readState: 'all' }, 50)).toBe(
      filterKey({ topics: ['b', 'a'], sources: null, readState: 'all' }, 50),
    );
  });

  it('does not mutate the arrays it is given', () => {
    const topics = ['b', 'a'];
    filterKey({ topics, sources: null, readState: 'all' }, 50);
    expect(topics).toEqual(['b', 'a']);
  });

  it('separates no override from an empty selection', () => {
    expect(filterKey({ topics: null, sources: null, readState: 'all' }, 50)).not.toBe(
      filterKey({ topics: [], sources: [], readState: 'all' }, 50),
    );
  });

  it('separates the two dimensions', () => {
    expect(filterKey({ topics: ['lwn'], sources: null, readState: 'all' }, 50)).not.toBe(
      filterKey({ topics: null, sources: ['lwn'], readState: 'all' }, 50),
    );
  });

  it('varies with the page size', () => {
    expect(filterKey(EMPTY_FILTERS, 25)).not.toBe(filterKey(EMPTY_FILTERS, 50));
  });

  it('does not alias a slug onto its own sentinels', () => {
    // The delimiter-joined version of this function used `*` for "no
    // override" and `+` between entries, so a slug of `*` aliased onto
    // "no override" and `a+b` aliased onto the pair `a`/`b`. Since
    // `usePagedResource` refetches only when the key changes, an alias
    // serves the previous selection's items under the new filter.
    //
    // This was reachable, not theoretical: no creation path constrains a
    // slug's shape — `add_source` and `add_topic` check uniqueness, and
    // the columns are plain `String(64)`.
    expect(filterKey({ topics: ['*'], sources: null, readState: 'all' }, 50)).not.toBe(
      filterKey({ topics: null, sources: null, readState: 'all' }, 50),
    );
    expect(filterKey({ topics: ['a+b'], sources: null, readState: 'all' }, 50)).not.toBe(
      filterKey({ topics: ['a', 'b'], sources: null, readState: 'all' }, 50),
    );
  });

  it('separates an empty selection from a slug that encodes as empty', () => {
    expect(filterKey({ topics: [], sources: null, readState: 'all' }, 50)).not.toBe(
      filterKey({ topics: [''], sources: null, readState: 'all' }, 50),
    );
  });

  // The trap. `usePagedResource` resets and refetches only when the key
  // changes, so a dimension missing from the key is a control that does
  // nothing: the user clicks "Unread", the URL changes, the request is
  // never made, and the cached pages stay on screen. Nothing throws and
  // nothing logs — the feed is simply wrong, quietly, and it looks like a
  // server-side filter that does not work.
  //
  // Measured: deleting `readState` from `filterKey` fails these three,
  // the narrowed-dimensions case, and the JSON-shape case below. Folding
  // it into a neighbouring array instead of giving it its own element
  // fails only the JSON-shape case, which is why that one is here.
  it.each([
    ['all', 'unread'],
    ['all', 'read'],
    ['unread', 'read'],
  ] as const)('changes between read state %s and %s', (left, right) => {
    expect(filterKey({ ...EMPTY_FILTERS, readState: left }, 50)).not.toBe(
      filterKey({ ...EMPTY_FILTERS, readState: right }, 50),
    );
  });

  it('changes with read state while the other dimensions are narrowed', () => {
    const base: FeedFilters = { topics: ['kernel'], sources: ['lwn'], readState: 'all' };
    expect(filterKey(base, 50)).not.toBe(filterKey({ ...base, readState: 'unread' }, 50));
  });

  it('does not alias read state onto a slug of the same name', () => {
    // The same failure the delimiter-joined version had, in the new
    // dimension: a topic called `unread` and a read state of `unread`
    // must not produce one cache entry. JSON encoding is what holds it —
    // `readState` is its own element rather than text concatenated with
    // its neighbours.
    expect(filterKey({ topics: ['unread'], sources: null, readState: 'all' }, 50)).not.toBe(
      filterKey({ topics: null, sources: null, readState: 'unread' }, 50),
    );
  });

  it('is still stable under selection order once read state is in the key', () => {
    expect(filterKey({ topics: ['a', 'b'], sources: null, readState: 'unread' }, 50)).toBe(
      filterKey({ topics: ['b', 'a'], sources: null, readState: 'unread' }, 50),
    );
  });

  it('stays parseable JSON', () => {
    // The property the delimiter version lost. Asserted rather than
    // assumed, because "encode it as JSON" is a claim about the output,
    // not about which function was called.
    expect(JSON.parse(filterKey({ topics: ['a'], sources: [], readState: 'read' }, 25))).toEqual([
      ['a'],
      [],
      'read',
      25,
    ]);
  });
});

// --- hasSavableOverride -------------------------------------------------

describe('hasSavableOverride', () => {
  // `PATCH /me/preferences` has fields for topics and sources and none for
  // read state — `user_preferences` has no column for it, and adding one
  // is a schema change. So "Save as my default" over a read-state-only
  // filter would send an empty patch and report success having stored
  // nothing. This is what keeps that button disabled.
  const CASES: [string, FeedFilters, boolean][] = [
    ['nothing set', EMPTY_FILTERS, false],
    ['read state only', { topics: null, sources: null, readState: 'unread' }, false],
    ['topics narrowed', { topics: ['kernel'], sources: null, readState: 'all' }, true],
    ['sources narrowed', { topics: null, sources: ['lwn'], readState: 'all' }, true],
    [
      'topics narrowed and read state set',
      { topics: ['kernel'], sources: null, readState: 'read' },
      true,
    ],
    ['everything deselected', { topics: [], sources: [], readState: 'all' }, true],
  ];

  it.each(CASES)('%s', (_label, filters, expected) => {
    expect(hasSavableOverride(filters)).toBe(expected);
  });

  it('is not the same question as hasOverride', () => {
    // The two diverge on exactly one shape, and that divergence is the
    // reason the function exists rather than reusing `hasOverride`.
    const readOnly: FeedFilters = { topics: null, sources: null, readState: 'unread' };
    expect(hasOverride(readOnly)).toBe(true);
    expect(hasSavableOverride(readOnly)).toBe(false);
  });
});

// --- effectiveSelection -------------------------------------------------

describe('effectiveSelection', () => {
  it('shows the saved selection when there is no override', () => {
    const result = effectiveSelection(
      EMPTY_FILTERS,
      preferences({ sources: ['lwn'], topics: ['kernel'] }),
      CATALOGUE,
    );
    expect(result).toEqual({ topics: ['kernel'], sources: ['lwn'] });
  });

  it('shows the whole catalogue when the saved selection is empty', () => {
    // An empty saved selection means the server applies instance defaults
    // — `_effective_sources` returns `selected or None`, and None narrows
    // nothing — so "everything" is what the chips must show.
    const result = effectiveSelection(EMPTY_FILTERS, preferences(), CATALOGUE);
    expect(result).toEqual({
      topics: ['kernel', 'security'],
      sources: ['lwn', 'hn', 'phoronix'],
    });
  });

  it('lets an override win over the saved selection', () => {
    const result = effectiveSelection(
      { topics: null, sources: ['phoronix'], readState: 'all' },
      preferences({ sources: ['lwn'], topics: ['kernel'] }),
      CATALOGUE,
    );
    expect(result).toEqual({ topics: ['kernel'], sources: ['phoronix'] });
  });

  it('honours an empty override rather than falling back', () => {
    // The asymmetry this whole file exists for. An empty *saved* selection
    // means "everything"; an empty *override* means "nothing". Same value,
    // opposite meanings, decided solely by which side of the `??` it is on.
    //
    // This also names a live trap in FilterBar's "Save as my default",
    // which writes `effective` into preferences: deselect every source and
    // save, and `[]` crosses from the override side to the saved side and
    // flips meaning — the user's "show me nothing" is stored as "show me
    // everything". The function is right; the caller needs a guard.
    const result = effectiveSelection(
      { topics: null, sources: [], readState: 'all' },
      preferences({ sources: ['lwn'], topics: ['kernel'] }),
      CATALOGUE,
    );
    expect(result).toEqual({ topics: ['kernel'], sources: [] });
  });

  it('falls back per dimension, not for the pair', () => {
    const result = effectiveSelection(
      { topics: ['security'], sources: null, readState: 'all' },
      preferences({ sources: [], topics: ['kernel'] }),
      CATALOGUE,
    );
    expect(result).toEqual({
      topics: ['security'],
      sources: ['lwn', 'hn', 'phoronix'],
    });
  });

  it('returns an empty catalogue fallback rather than throwing', () => {
    const result = effectiveSelection(EMPTY_FILTERS, preferences(), { sources: [], topics: [] });
    expect(result).toEqual({ topics: [], sources: [] });
  });
});

// --- toggle -------------------------------------------------------------

describe('toggle', () => {
  it('adds a value that is absent, at the end', () => {
    expect(toggle(['a', 'b'], 'c')).toEqual(['a', 'b', 'c']);
  });

  it('removes a value that is present, preserving the rest in order', () => {
    expect(toggle(['a', 'b', 'c'], 'b')).toEqual(['a', 'c']);
  });

  it('does not mutate the array it is given', () => {
    const values = ['a'];
    toggle(values, 'b');
    toggle(values, 'a');
    expect(values).toEqual(['a']);
  });

  it('removes every copy of a duplicated value', () => {
    // `parseFilters` does not de-duplicate, so a hand-edited URL can seed
    // duplicates. One click clears them all rather than half of them.
    expect(toggle(['a', 'a', 'b'], 'a')).toEqual(['b']);
  });

  it('turns an empty selection into a single choice', () => {
    // The path out of "None": the empty array is a real selection, so
    // toggling appends to it rather than starting from the catalogue.
    expect(toggle([], 'a')).toEqual(['a']);
  });
});
