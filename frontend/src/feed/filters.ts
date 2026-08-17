import type { Preferences, Source, Topic } from '../api/types';

/**
 * `null` means "no override — let the server use my saved selection",
 * which is exactly what omitting the query parameter does. An empty array
 * is different: it means the user has deselected everything, and nothing
 * can match, so the feed short-circuits without a request.
 */
export interface FeedFilters {
  topics: string[] | null;
  sources: string[] | null;
}

export const EMPTY_FILTERS: FeedFilters = { topics: null, sources: null };

function parseList(raw: string | null): string[] | null {
  if (raw === null) return null;
  return raw
    .split(',')
    .map((value) => value.trim())
    .filter((value) => value.length > 0);
}

export function parseFilters(params: URLSearchParams): FeedFilters {
  return {
    topics: parseList(params.get('topics')),
    sources: parseList(params.get('sources')),
  };
}

export function applyFiltersToParams(params: URLSearchParams, filters: FeedFilters): URLSearchParams {
  const next = new URLSearchParams(params);
  for (const dimension of ['topics', 'sources'] as const) {
    const value = filters[dimension];
    if (value === null) next.delete(dimension);
    else next.set(dimension, value.join(','));
  }
  return next;
}

export function hasOverride(filters: FeedFilters): boolean {
  return filters.topics !== null || filters.sources !== null;
}

/** Nothing can match, so the feed renders an empty state without a fetch. */
export function selectsNothing(filters: FeedFilters): boolean {
  return filters.topics?.length === 0 || filters.sources?.length === 0;
}

/** Stable identity for the paged-resource cache key. */
export function filterKey(filters: FeedFilters, limit: number): string {
  const part = (value: string[] | null) => (value === null ? '*' : [...value].sort().join('+'));
  return `${part(filters.topics)}|${part(filters.sources)}|${limit}`;
}

/**
 * What the feed is *actually* showing right now, so a chip UI has
 * something to toggle before the user has set any override. An empty
 * saved selection means the server falls back to instance defaults, which
 * we render as "everything in the catalogue".
 */
export function effectiveSelection(
  filters: FeedFilters,
  preferences: Preferences,
  catalogue: { sources: Source[]; topics: Topic[] },
): { topics: string[]; sources: string[] } {
  const savedTopics =
    preferences.topics.length > 0 ? preferences.topics : catalogue.topics.map((topic) => topic.slug);
  const savedSources =
    preferences.sources.length > 0
      ? preferences.sources
      : catalogue.sources.map((source) => source.slug);
  return {
    topics: filters.topics ?? savedTopics,
    sources: filters.sources ?? savedSources,
  };
}

export function toggle(values: string[], value: string): string[] {
  return values.includes(value) ? values.filter((entry) => entry !== value) : [...values, value];
}
