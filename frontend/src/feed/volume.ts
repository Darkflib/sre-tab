/**
 * Publication volume is wildly uneven across the catalogue: general-news
 * sources publish an order of magnitude faster than the small technical
 * ones, so a purely time-ordered feed is theirs within the hour. Two
 * signals drive the UI's response to that.
 */
import type { FeedItem, Source } from '../api/types';

/**
 * Signal one, available before any item loads: the operator's refresh
 * interval is a direct proxy for cadence. Fifteen minutes is the fastest
 * tier in the catalogue and marks the sources that will dominate.
 */
export const HIGH_VOLUME_REFRESH_MINUTES = 15;

export function isHighVolume(source: Source): boolean {
  return source.refresh_minutes <= HIGH_VOLUME_REFRESH_MINUTES;
}

/**
 * Signal two, measured rather than assumed: one source taking more than
 * this share of what has actually loaded is worth telling the user about.
 */
export const DOMINANCE_THRESHOLD = 0.35;

export interface SourceShare {
  slug: string;
  name: string;
  count: number;
  /** 0–1, of the currently loaded items. */
  share: number;
}

export function computeShares(items: FeedItem[]): SourceShare[] {
  if (items.length === 0) return [];
  const counts = new Map<string, SourceShare>();
  for (const item of items) {
    const existing = counts.get(item.source.slug);
    if (existing) existing.count += 1;
    else counts.set(item.source.slug, { slug: item.source.slug, name: item.source.name, count: 1, share: 0 });
  }
  const shares = [...counts.values()];
  for (const entry of shares) entry.share = entry.count / items.length;
  shares.sort((a, b) => b.count - a.count || a.name.localeCompare(b.name, 'en-GB'));
  return shares;
}

export function findDominantSource(shares: SourceShare[], loadedCount: number): SourceShare | null {
  // Below a dozen items the ratio is noise, not dominance.
  if (loadedCount < 12) return null;
  if (shares.length < 2) return null;
  const leader = shares[0];
  return leader.share >= DOMINANCE_THRESHOLD ? leader : null;
}
