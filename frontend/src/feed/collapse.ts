import { readStoredValue, writeStoredValue } from '../lib/storage';
import type { FeedFilters } from './filters';

/**
 * Whether the filter bar is collapsed, and what it has to say while it is.
 *
 * Kept per device rather than in `user_preferences`, because the question
 * has a different answer on a phone and on a desktop and the v1 schema has
 * no row that can hold two — `ROADMAP.md` puts per-device preferences at
 * v2. It is a view state rather than a preference the server has an opinion
 * about, so it stays out of the database entirely: no column, no migration,
 * no API change. `theme.ts` already mirrors a choice into `localStorage` on
 * the same terms, and both go through `lib/storage.ts` so there is one
 * place where a storage that throws is handled.
 */
export const FILTERS_COLLAPSED_STORAGE_KEY = 'dnd.filters.collapsed';

/** The stored spellings. Words rather than `true`/`false`, so the value is
 *  self-describing in devtools and anything else — a stale key, another tab
 *  writing nonsense — falls to the default below. */
const COLLAPSED = 'collapsed';
const EXPANDED = 'expanded';

/**
 * Expanded is the default, and the *absence* of a stored value is what says
 * so: nobody's first visit changes. Storage that throws lands on the same
 * answer as storage that is empty, which is the point of the default being
 * the safe one — a reader who cannot persist anything gets the bar they had
 * before this existed, not a hidden one they never asked for.
 */
export function readFiltersCollapsed(): boolean {
  return readStoredValue(FILTERS_COLLAPSED_STORAGE_KEY) === COLLAPSED;
}

export function rememberFiltersCollapsed(collapsed: boolean): void {
  writeStoredValue(FILTERS_COLLAPSED_STORAGE_KEY, collapsed ? COLLAPSED : EXPANDED);
}

/** Enough of a `Source` or a `Topic` to name it. */
export interface NamedEntry {
  slug: string;
  name: string;
}

/** Past this many, the summary counts the rest instead of listing them. */
const MAX_NAMED = 3;

/**
 * What is narrowing the feed, in words, for a bar whose chips are hidden.
 *
 * A collapsed filter bar with three sources selected is still filtering,
 * and a reader who has forgotten that reads the short feed as a bug. The
 * counts in the "Filtered" badge cannot close that on their own — "3 of 8
 * sources" does not say *which* three, and the chips that would are exactly
 * what is off screen.
 *
 * The three states of a dimension stay three states here, because flattening
 * them is how the whole filter model gets lied about:
 *
 * - `null` is **not** a narrowing. It means "no override, use my saved
 *   selection", so it contributes no phrase at all and an unfiltered bar
 *   summarises to nothing — the "Your selection" badge is already the
 *   accurate thing to say, and adding a phrase would tell a reader they are
 *   filtering when they are not.
 * - `[]` is the loudest case rather than the quietest: nothing can match, so
 *   it is named outright.
 * - A list is named, up to `MAX_NAMED`, then counted.
 *
 * Read state is in here even though the badge line also mentions it. This
 * function answers "what is narrowing this feed", and an answer that
 * silently omits a dimension is the same flattening in a different place.
 */
export function summariseFilters(
  filters: FeedFilters,
  catalogue: { sources: NamedEntry[]; topics: NamedEntry[] },
): string[] {
  const parts: string[] = [];

  const sources = describeDimension(filters.sources, catalogue.sources, 'Sources');
  if (sources !== null) parts.push(sources);

  const topics = describeDimension(filters.topics, catalogue.topics, 'Topics');
  if (topics !== null) parts.push(topics);

  if (filters.readState !== 'all') {
    parts.push(filters.readState === 'unread' ? 'Unread only' : 'Read only');
  }

  return parts;
}

function describeDimension(
  selection: string[] | null,
  catalogue: NamedEntry[],
  heading: 'Sources' | 'Topics',
): string | null {
  if (selection === null) return null;
  if (selection.length === 0) return `No ${heading.toLowerCase()} selected`;

  const names = new Map(catalogue.map((entry) => [entry.slug, entry.name]));
  // The slug is the fallback rather than a silent drop. A selection can name
  // a source the catalogue no longer carries — a shared link, a disabled
  // source — and that slug is still narrowing the feed, so saying it is
  // closer to the truth than saying nothing and shortening the list.
  const labels = selection.map((slug) => names.get(slug) ?? slug);

  if (labels.length <= MAX_NAMED) return `${heading}: ${sentenceList(labels)}`;
  const named = labels.slice(0, MAX_NAMED);
  return `${heading}: ${sentenceList([...named, `${labels.length - MAX_NAMED} more`])}`;
}

/** UK English, so the last separator is "and" and the comma before it stays. */
function sentenceList(values: string[]): string {
  if (values.length < 3) return values.join(' and ');
  return `${values.slice(0, -1).join(', ')}, and ${values[values.length - 1]}`;
}
