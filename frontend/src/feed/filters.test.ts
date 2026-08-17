import { describe, expect, it } from 'vitest';

import type { Preferences, Source, Topic } from '../api/types';
import {
  applyFiltersToParams,
  effectiveSelection,
  EMPTY_FILTERS,
  type FeedFilters,
  filterKey,
  hasOverride,
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
    expect(parseFilters(new URLSearchParams('topics='))).toEqual({ topics: [], sources: null });
  });

  it('reads the two dimensions independently', () => {
    expect(parseFilters(new URLSearchParams('topics=kernel'))).toEqual({
      topics: ['kernel'],
      sources: null,
    });
    expect(parseFilters(new URLSearchParams('sources=lwn'))).toEqual({
      topics: null,
      sources: ['lwn'],
    });
  });

  it('splits on commas, trims, and drops blanks', () => {
    expect(parseFilters(new URLSearchParams('topics=%20kernel%20,,%20security%20'))).toEqual({
      topics: ['kernel', 'security'],
      sources: null,
    });
  });

  it('treats a parameter of only separators as an empty selection', () => {
    // Every entry is dropped as blank, leaving `[]` — which is "nothing
    // selected", not "no override". A hand-edited URL reaches this.
    expect(parseFilters(new URLSearchParams('sources=,,,'))).toEqual({
      topics: null,
      sources: [],
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
    const next = applyFiltersToParams(new URLSearchParams(''), { topics: [], sources: null });
    expect(next.get('topics')).toBe('');
    expect(next.toString()).toBe('topics=');
  });

  it('preserves unrelated parameters', () => {
    const next = applyFiltersToParams(new URLSearchParams('view=compact'), {
      topics: ['kernel'],
      sources: null,
    });
    expect(next.get('view')).toBe('compact');
    expect(next.get('topics')).toBe('kernel');
  });

  it('does not mutate the parameters it is given', () => {
    const original = new URLSearchParams('topics=kernel');
    applyFiltersToParams(original, { topics: [], sources: ['lwn'] });
    expect(original.toString()).toBe('topics=kernel');
  });
});

// --- the round trip -----------------------------------------------------

const ROUND_TRIPS: [string, FeedFilters][] = [
  ['no override at all', { topics: null, sources: null }],
  ['a topic override only', { topics: ['kernel'], sources: null }],
  ['a source override only', { topics: null, sources: ['lwn'] }],
  ['both dimensions overridden', { topics: ['kernel', 'security'], sources: ['lwn', 'hn'] }],
  ['everything deselected', { topics: [], sources: [] }],
  ['one dimension deselected, the other untouched', { topics: [], sources: null }],
  ['one dimension deselected, the other narrowed', { topics: [], sources: ['lwn'] }],
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
    });
    expect(parseFilters(params)).toEqual({ topics: null, sources: [] });
  });

  it('does not survive a slug containing a comma — a documented assumption', () => {
    // The comma is the separator and nothing escapes it, so this is a real
    // limitation rather than a latent bug: slugs are kebab-case, generated
    // by the CLI, and cannot contain one. The test exists so that changing
    // the slug rules fails here rather than in the feed.
    const params = applyFiltersToParams(new URLSearchParams(''), {
      topics: null,
      sources: ['a,b'],
    });
    expect(parseFilters(params).sources).toEqual(['a', 'b']);
  });
});

// --- hasOverride / selectsNothing ---------------------------------------

const OVERRIDE_CASES: [string, FeedFilters, boolean][] = [
  ['neither dimension set', { topics: null, sources: null }, false],
  ['topics narrowed', { topics: ['kernel'], sources: null }, true],
  ['sources narrowed', { topics: null, sources: ['lwn'] }, true],
  ['topics deselected entirely', { topics: [], sources: null }, true],
  ['both set', { topics: [], sources: ['lwn'] }, true],
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
  ['no override', { topics: null, sources: null }, false],
  ['both narrowed to something', { topics: ['kernel'], sources: ['lwn'] }, false],
  ['topics deselected', { topics: [], sources: null }, true],
  ['sources deselected', { topics: null, sources: [] }, true],
  ['sources deselected while topics are narrowed', { topics: ['kernel'], sources: [] }, true],
  ['both deselected', { topics: [], sources: [] }, true],
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
    expect(filterKey({ topics: ['a', 'b'], sources: null }, 50)).toBe(
      filterKey({ topics: ['b', 'a'], sources: null }, 50),
    );
  });

  it('does not mutate the arrays it is given', () => {
    const topics = ['b', 'a'];
    filterKey({ topics, sources: null }, 50);
    expect(topics).toEqual(['b', 'a']);
  });

  it('separates no override from an empty selection', () => {
    expect(filterKey({ topics: null, sources: null }, 50)).not.toBe(
      filterKey({ topics: [], sources: [] }, 50),
    );
  });

  it('separates the two dimensions', () => {
    expect(filterKey({ topics: ['lwn'], sources: null }, 50)).not.toBe(
      filterKey({ topics: null, sources: ['lwn'] }, 50),
    );
  });

  it('varies with the page size', () => {
    expect(filterKey(EMPTY_FILTERS, 25)).not.toBe(filterKey(EMPTY_FILTERS, 50));
  });

  it('collides on slugs that use its own sentinels — a documented assumption', () => {
    // `*` marks "no override" and `+` joins the selection, so a slug that
    // is literally `*`, or one containing `+`, would alias to another key
    // and serve a cached page for the wrong filter. Neither is reachable
    // with kebab-case slugs. Pinned so that loosening the slug rules fails
    // here, where the reason is written down, rather than as a stale feed.
    expect(filterKey({ topics: ['*'], sources: null }, 50)).toBe(
      filterKey({ topics: null, sources: null }, 50),
    );
    expect(filterKey({ topics: ['a+b'], sources: null }, 50)).toBe(
      filterKey({ topics: ['a', 'b'], sources: null }, 50),
    );
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
      { topics: null, sources: ['phoronix'] },
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
      { topics: null, sources: [] },
      preferences({ sources: ['lwn'], topics: ['kernel'] }),
      CATALOGUE,
    );
    expect(result).toEqual({ topics: ['kernel'], sources: [] });
  });

  it('falls back per dimension, not for the pair', () => {
    const result = effectiveSelection(
      { topics: ['security'], sources: null },
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
