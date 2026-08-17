import { describe, expect, it } from 'vitest';

import type { FeedItem, Source } from '../api/types';
import {
  computeShares,
  DOMINANCE_THRESHOLD,
  findDominantSource,
  HIGH_VOLUME_REFRESH_MINUTES,
  isHighVolume,
  type SourceShare,
} from './volume';

/**
 * Two signals, deliberately different in kind: `isHighVolume` is a property
 * of the operator's configuration and is known before anything loads, while
 * the share arithmetic is measured from the items actually on screen. Both
 * feed UI that tells the user something about their own feed, so both are
 * wrong in the same expensive way — a threshold that quietly stops firing
 * looks exactly like a feed that happens to be balanced.
 *
 * The thresholds themselves (15 minutes, 35%, 12 items) are judgement calls
 * with no test that can prove them right. What is pinned here is that they
 * are the values in force and that the comparisons around them are the ones
 * intended — inclusive where they read as inclusive, and guarded where a
 * ratio would otherwise be computed from too little data.
 */

// --- fixtures -----------------------------------------------------------

function source(slug: string, refreshMinutes: number): Source {
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

interface Spec {
  slug: string;
  count: number;
  name?: string;
}

function itemsFor(specs: Spec[]): FeedItem[] {
  const items: FeedItem[] = [];
  let id = 0;
  for (const spec of specs) {
    for (let i = 0; i < spec.count; i += 1) {
      id += 1;
      items.push({
        id,
        canonical_url: `https://example.invalid/${spec.slug}/${i}`,
        title: `${spec.slug} item ${i}`,
        summary: null,
        image_url: null,
        published_at: '2026-08-17T09:00:00Z',
        source: { slug: spec.slug, name: spec.name ?? spec.slug.toUpperCase(), icon_url: null },
        topics: [],
        read: false,
        bookmarked: false,
      });
    }
  }
  return items;
}

// --- isHighVolume -------------------------------------------------------

const REFRESH_CASES: [number, boolean][] = [
  [5, true],
  [10, true],
  [HIGH_VOLUME_REFRESH_MINUTES, true],
  [HIGH_VOLUME_REFRESH_MINUTES + 1, false],
  [30, false],
  [60, false],
  [1440, false],
];

describe('isHighVolume', () => {
  it.each(REFRESH_CASES)('%i minutes → %s', (minutes, expected) => {
    expect(isHighVolume(source('s', minutes))).toBe(expected);
  });

  it('is inclusive at the threshold', () => {
    // Fifteen minutes is the fastest tier the catalogue actually uses, so
    // an exclusive comparison would flag nothing at all — the failure
    // would be an absence of warnings, which nobody notices.
    expect(isHighVolume(source('s', HIGH_VOLUME_REFRESH_MINUTES))).toBe(true);
  });
});

// --- computeShares ------------------------------------------------------

describe('computeShares', () => {
  it('returns nothing for an empty feed', () => {
    expect(computeShares([])).toEqual([]);
  });

  it('counts per source and divides by what is loaded', () => {
    const shares = computeShares(itemsFor([{ slug: 'hn', count: 3 }, { slug: 'lwn', count: 1 }]));
    expect(shares).toEqual([
      { slug: 'hn', name: 'HN', count: 3, share: 0.75 },
      { slug: 'lwn', name: 'LWN', count: 1, share: 0.25 },
    ]);
  });

  it('produces shares that sum to one', () => {
    const shares = computeShares(
      itemsFor([{ slug: 'a', count: 1 }, { slug: 'b', count: 1 }, { slug: 'c', count: 1 }]),
    );
    const total = shares.reduce((sum, entry) => sum + entry.share, 0);
    expect(total).toBeCloseTo(1, 10);
  });

  it('orders by count, descending', () => {
    const shares = computeShares(
      itemsFor([{ slug: 'small', count: 1 }, { slug: 'big', count: 5 }, { slug: 'mid', count: 3 }]),
    );
    expect(shares.map((entry) => entry.slug)).toEqual(['big', 'mid', 'small']);
  });

  it('breaks ties by name so the panel does not reshuffle', () => {
    // Insertion order follows whatever the page happened to return, so
    // without the name tie-break two equal sources would swap places as
    // items load and the composition list would jitter.
    const shares = computeShares(
      itemsFor([
        { slug: 'z', count: 2, name: 'Zeta' },
        { slug: 'a', count: 2, name: 'Alpha' },
      ]),
    );
    expect(shares.map((entry) => entry.name)).toEqual(['Alpha', 'Zeta']);
  });

  it('takes the display name from the item, not the catalogue', () => {
    const shares = computeShares(itemsFor([{ slug: 'lwn', count: 1, name: 'LWN.net' }]));
    expect(shares[0].name).toBe('LWN.net');
  });

  it('gives a single source the whole feed', () => {
    const shares = computeShares(itemsFor([{ slug: 'only', count: 4 }]));
    expect(shares).toEqual([{ slug: 'only', name: 'ONLY', count: 4, share: 1 }]);
  });

  it('does not mutate the items it is given', () => {
    const items = itemsFor([{ slug: 'a', count: 2 }]);
    const before = structuredClone(items);
    computeShares(items);
    expect(items).toEqual(before);
  });
});

// --- findDominantSource -------------------------------------------------

/** Shares are built through `computeShares` so the pair stays consistent. */
function sharesFor(specs: Spec[]): { shares: SourceShare[]; loaded: number } {
  const items = itemsFor(specs);
  return { shares: computeShares(items), loaded: items.length };
}

describe('findDominantSource', () => {
  it('says nothing below a dozen items', () => {
    // Eleven items is not evidence of anything: one source holding 8 of
    // them is as likely to be publication timing as dominance.
    const { shares, loaded } = sharesFor([{ slug: 'hn', count: 8 }, { slug: 'lwn', count: 3 }]);
    expect(loaded).toBe(11);
    expect(findDominantSource(shares, loaded)).toBeNull();
  });

  it('starts answering at exactly a dozen', () => {
    const { shares, loaded } = sharesFor([{ slug: 'hn', count: 8 }, { slug: 'lwn', count: 4 }]);
    expect(loaded).toBe(12);
    expect(findDominantSource(shares, loaded)?.slug).toBe('hn');
  });

  it('says nothing when only one source is present, at any share', () => {
    // A feed filtered to one source is 100% that source by construction.
    // Telling the user it dominates would be noise, and the "Only" button
    // in the notice would be a no-op.
    const { shares, loaded } = sharesFor([{ slug: 'hn', count: 40 }]);
    expect(shares[0].share).toBe(1);
    expect(findDominantSource(shares, loaded)).toBeNull();
  });

  it('says nothing for an empty feed', () => {
    expect(findDominantSource([], 0)).toBeNull();
  });

  it('returns the leader at exactly the threshold', () => {
    // 7 of 20 is 0.35 exactly. The comparison is `>=`, and a float that
    // lands precisely on the boundary is the case most likely to flip if
    // someone rewrites it.
    const { shares, loaded } = sharesFor([
      { slug: 'hn', count: 7 },
      { slug: 'lwn', count: 7 },
      { slug: 'phoronix', count: 6 },
    ]);
    expect(shares[0].share).toBeCloseTo(DOMINANCE_THRESHOLD, 10);
    expect(findDominantSource(shares, loaded)?.slug).toBe('hn');
  });

  it('says nothing when the leader is just under the threshold', () => {
    // 6 of 20 is 0.30.
    const { shares, loaded } = sharesFor([
      { slug: 'hn', count: 6 },
      { slug: 'lwn', count: 5 },
      { slug: 'phoronix', count: 5 },
      { slug: 'other', count: 4 },
    ]);
    expect(shares[0].share).toBeLessThan(DOMINANCE_THRESHOLD);
    expect(findDominantSource(shares, loaded)).toBeNull();
  });

  it('returns the leader, not merely a source over the threshold', () => {
    const { shares, loaded } = sharesFor([{ slug: 'lwn', count: 7 }, { slug: 'hn', count: 13 }]);
    expect(findDominantSource(shares, loaded)?.slug).toBe('hn');
  });

  it('trusts the loaded count it is given over the shares it is given', () => {
    // FeedPage passes `feed.entries.length` alongside shares computed from
    // the same array, so the two always agree there. Pinned because the
    // guard reads the argument rather than summing the shares, which is
    // only safe while that stays true.
    const { shares } = sharesFor([{ slug: 'hn', count: 40 }, { slug: 'lwn', count: 4 }]);
    expect(findDominantSource(shares, 11)).toBeNull();
  });
});
