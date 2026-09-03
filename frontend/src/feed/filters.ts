import type { Preferences, ReadFilter, Source, Topic } from '../api/types';

/**
 * `null` means "no override — let the server use my saved selection",
 * which is exactly what omitting the query parameter does. An empty array
 * is different: it means the user has deselected everything, and nothing
 * can match, so the feed short-circuits without a request.
 *
 * `readState` is deliberately *not* that shape. The array dimensions are
 * three-state because they have a saved selection to fall back to; read
 * state has none — there is no column for it in `user_preferences` — so
 * "no narrowing" is a value it can hold rather than an absence it has to
 * signal. Two states behind two representations, and a `null` here would
 * mean the same thing as `'all'` while looking like it meant something
 * else.
 */
export interface FeedFilters {
  topics: string[] | null;
  sources: string[] | null;
  readState: ReadFilter;
  /**
   * Two states behind one representation, for the same reason `readState`
   * has: there is no saved counterpart to fall back to, so "not searching"
   * is a value this can hold — the empty string — rather than an absence it
   * has to signal. A `null` here would mean what `''` means while looking
   * like it meant something else.
   */
  query: string;
}

export const EMPTY_FILTERS: FeedFilters = {
  topics: null,
  sources: null,
  readState: 'all',
  query: '',
};

/** The URL and API spelling of `readState`; the two agree on purpose. */
export const READ_STATE_PARAM = 'read_state';

/**
 * The URL and API spelling of `query`. Short because a reader sees it and
 * may well type it, and `q` is what every other search box in the world
 * puts there.
 */
export const QUERY_PARAM = 'q';

/**
 * Matches `FEED_MAX_QUERY_LENGTH` on the server, which answers anything
 * longer with a 422. Enforced here as a `maxLength` on the input so the
 * reader meets a text box that stops rather than an error banner over the
 * whole feed.
 */
export const MAX_QUERY_LENGTH = 200;

/**
 * A `Record` keyed by the union rather than a list of strings: adding a
 * member to `ReadFilter` server-side then fails the typecheck here, which
 * is the whole reason the type is generated from `openapi.json`. A plain
 * `string[]` would compile against a vocabulary it no longer knows.
 */
const READ_FILTERS: Record<ReadFilter, true> = { all: true, unread: true, read: true };

function parseReadState(raw: string | null): ReadFilter {
  // An unrecognised value reads as `all` rather than being passed on. The
  // server answers a value outside the enum with a 422, so forwarding a
  // hand-edited `?read_state=nonsense` would turn a typo in the URL into
  // an error banner over the whole feed.
  if (raw !== null && Object.hasOwn(READ_FILTERS, raw)) return raw as ReadFilter;
  return 'all';
}

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
    readState: parseReadState(params.get(READ_STATE_PARAM)),
    // Trimmed and bounded on the way in, so a hand-edited or shared URL
    // carrying whitespace or 4KB of text becomes a query the server will
    // answer rather than a 422 the reader has to decode.
    query: (params.get(QUERY_PARAM) ?? '').trim().slice(0, MAX_QUERY_LENGTH),
  };
}

export function applyFiltersToParams(params: URLSearchParams, filters: FeedFilters): URLSearchParams {
  const next = new URLSearchParams(params);
  for (const dimension of ['topics', 'sources'] as const) {
    const value = filters[dimension];
    if (value === null) next.delete(dimension);
    else next.set(dimension, value.join(','));
  }
  // Deleted rather than written as `read_state=all`: `all` is the default
  // on both sides, so writing it would put a parameter in every shared
  // link that says nothing.
  if (filters.readState === 'all') next.delete(READ_STATE_PARAM);
  else next.set(READ_STATE_PARAM, filters.readState);
  // Same reasoning again: an empty search is the default on both sides, so
  // writing `q=` would put a parameter in every shared link that says
  // nothing.
  if (filters.query === '') next.delete(QUERY_PARAM);
  else next.set(QUERY_PARAM, filters.query);
  return next;
}

export function hasOverride(filters: FeedFilters): boolean {
  // Read state counts: it drives the "Filtered" badge, and the badge is
  // what puts "Clear filters" on screen. Leave it out and a user who has
  // narrowed to unread sees an unexplained short feed with no way back.
  return (
    filters.topics !== null ||
    filters.sources !== null ||
    filters.readState !== 'all' ||
    filters.query !== ''
  );
}

/**
 * Nothing can match, so the feed renders an empty state without a fetch.
 *
 * Read state is absent from this on purpose. The claim here is that no
 * item *can* match — knowable locally only because an empty selection
 * intersects everything to nothing. "Unread only" can return nothing, but
 * whether it does is a fact about the database, and asserting it here
 * would render an empty state over a feed that has items.
 */
export function selectsNothing(filters: FeedFilters): boolean {
  return filters.topics?.length === 0 || filters.sources?.length === 0;
}

/**
 * Whether these filters describe something `PATCH /me/preferences` can
 * store. Read state cannot be: `user_preferences` has no column for it,
 * and inventing one is a schema change. It is a per-session URL filter,
 * so a view narrowed only by read state has nothing to save.
 */
export function hasSavableOverride(filters: FeedFilters): boolean {
  return filters.topics !== null || filters.sources !== null;
}

/**
 * Stable identity for the paged-resource cache key.
 *
 * Encoded as JSON rather than joined with delimiters. The delimiter version
 * used `*` for "no override" and `+` between entries, which aliased: a slug
 * of `*`, or the pair `a`/`b` against the single slug `a+b`, produced the
 * same key. `usePagedResource` only resets and refetches when the key
 * changes, so an alias serves the previous selection's items for the new
 * filter — a wrong feed, silently. Nothing constrains a slug's format at any
 * creation path (`app/cli/operations.py` checks uniqueness, not shape), so
 * that was reachable rather than theoretical.
 *
 * Every dimension the request depends on has to be in here, `readState`
 * included: `usePagedResource` resets and refetches only when the key
 * changes, so a filter left out of the key is a control that silently
 * does nothing — the user clicks "Unread" and the cached pages stay.
 */
export function filterKey(filters: FeedFilters, limit: number): string {
  const part = (value: string[] | null) => (value === null ? null : [...value].sort());
  return JSON.stringify([
    part(filters.topics),
    part(filters.sources),
    filters.readState,
    filters.query,
    limit,
  ]);
}

/**
 * The muted terms that guarantee this search returns nothing.
 *
 * Not a heuristic. A search requires every one of its words; a mute
 * excludes any item carrying all of the mute's words. So if a muted
 * term's words are a subset of the query's, every item the search could
 * match is also an item the mute removes, and the page is provably empty
 * before the request is made.
 *
 * It exists because that is the one case where muting is actively
 * confusing rather than merely invisible: the reader types a word, gets
 * nothing, and has no way to tell an empty corpus from their own standing
 * preference. Everything else the mute hides, they were not looking for.
 *
 * Case-folded to match the server, which normalises terms the same way
 * before storing them.
 */
export function mutesBlocking(query: string, mutedWords: string[]): string[] {
  const asked = new Set(query.toLowerCase().split(/\s+/).filter(Boolean));
  // No early return for an empty query. `every` over a term's words is
  // already false when none of them were asked for, and `words.length > 0`
  // covers the vacuous case — a guard was written here, removing it broke
  // no test, and it was removed rather than kept as decoration.
  return mutedWords.filter((term) => {
    const words = term.toLowerCase().split(/\s+/).filter(Boolean);
    return words.length > 0 && words.every((word) => asked.has(word));
  });
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
