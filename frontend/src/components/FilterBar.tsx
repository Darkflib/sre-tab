import { useId, useState } from 'react';

import type { PreferencesPatch, ReadFilter } from '../api/types';
import { useCatalogue } from '../catalogue/useCatalogue';
import {
  readFiltersCollapsed,
  rememberFiltersCollapsed,
  summariseFilters,
} from '../feed/collapse';
import {
  effectiveSelection,
  EMPTY_FILTERS,
  type FeedFilters,
  hasOverride,
  hasSavableOverride,
  mutesBlocking,
  selectsNothing,
  shouldReplaceHistory,
  toggle,
} from '../feed/filters';
import { isHighVolume, type SourceShare } from '../feed/volume';
import { cssVars } from '../lib/css';
import { percent } from '../lib/format';
import { useAuthenticatedSession } from '../session/useSession';
import { Link } from 'react-router-dom';

import { ChevronIcon, CrossIcon } from './icons';
import { SearchBox } from './SearchBox';

interface FilterBarProps {
  filters: FeedFilters;
  /**
   * `replace` asks the router to replace the current history entry rather
   * than push one. Typing is the reason it exists: a debounced search
   * commits every few hundred milliseconds, and each commit pushing an
   * entry would make Back walk the reader backwards through their own
   * keystrokes instead of out of the search.
   */
  onChange: (next: FeedFilters, options?: { replace?: boolean }) => void;
  /** Measured from the items actually loaded, not assumed. */
  shares: SourceShare[];
  loadedCount: number;
}

/**
 * Filtering is the primary control on this screen, not a secondary one:
 * the catalogue mixes sources whose publication rates differ by an order
 * of magnitude, so an unfiltered time-ordered feed belongs to the fastest
 * of them within the hour. Hence chips in the page flow rather than behind
 * a menu, a measured composition breakdown, and one-click narrowing.
 *
 * That argument is about where the control lives, not about how much of the
 * viewport it is entitled to once a reader has used it. So the chips
 * collapse, and the head — badge, counts, and the way back out — never
 * does: a bar that hid what it was doing would be the failure this whole
 * file is written against, just quieter.
 */
export function FilterBar({ filters, onChange, shares, loadedCount }: FilterBarProps) {
  const { sources, topics } = useCatalogue();
  const { preferences, updatePreferences } = useAuthenticatedSession();

  const effective = effectiveSelection(filters, preferences, { sources, topics });
  const overridden = hasOverride(filters);
  const countBySlug = new Map(shares.map((entry) => [entry.slug, entry.count]));

  const bodyId = useId();
  // Read once on mount, so the answer survives a remount without the server
  // knowing anything about it. Expanded when nothing is stored, and when
  // storage cannot be read at all.
  const [collapsed, setCollapsed] = useState(readFiltersCollapsed);

  const toggleCollapsed = () => {
    const next = !collapsed;
    setCollapsed(next);
    // Written outside the state updater on purpose: StrictMode calls that
    // twice, and a side effect belongs with the event rather than with the
    // render it schedules.
    rememberFiltersCollapsed(next);
  };

  // Names, which the counts in the badge cannot give. Rendered only while
  // the chips are hidden, because the chips are the better version of it.
  const summary = summariseFilters(filters, { sources, topics });

  // Muting is the one narrowing with no evidence of itself on this screen:
  // a deselected source is a chip you can see and a search is text in a
  // box, but a mute simply removes items. So it is stated here whenever it
  // is on, outside the disclosure, with the way to change it attached.
  const mutedCount = preferences.muted_words.length + preferences.muted_tags.length;
  const blocking = mutesBlocking(filters.query, preferences.muted_words);

  const setSources = (next: string[] | null) => {
    onChange({ ...filters, sources: next });
  };
  const setTopics = (next: string[] | null) => {
    onChange({ ...filters, topics: next });
  };
  const setReadState = (next: ReadFilter) => {
    onChange({ ...filters, readState: next });
  };
  const setQuery = (next: string) => {
    onChange({ ...filters, query: next }, { replace: shouldReplaceHistory(filters.query, next) });
  };

  // Nothing selected is a step, not a destination: it exists so you can
  // clear the chips and pick two, rather than deselecting sixteen. It also
  // cannot be saved — an empty saved selection is how the server spells
  // "no preference, use the instance defaults", so storing it would mean
  // the opposite of what the button says.
  const nothingSelected = selectsNothing(filters);

  // A second reason the button can be dead, and a different one. Read
  // state has no home in `user_preferences`, so narrowing to unread is a
  // filter on this URL and nothing more. Without this guard "Save as my
  // default" appears the moment the read chip is clicked and saves an
  // empty patch — a control that reports success and stores nothing.
  const savable = hasSavableOverride(filters);

  const saveAsDefault = () => {
    // Save what the user chose, not what the chips are showing. `effective`
    // resolves an un-overridden dimension into the full catalogue for
    // rendering, and writing that back would convert "follow the instance"
    // into a pinned snapshot of today's catalogue — after which a source
    // added later would never appear for this user, with nothing to
    // indicate why. An absent field leaves that dimension alone.
    const patch: PreferencesPatch = {};
    if (filters.topics !== null) patch.topics = filters.topics;
    if (filters.sources !== null) patch.sources = filters.sources;

    void updatePreferences(patch).then(() => {
      // Read state clears with the rest: it was never part of what was
      // saved, so leaving it on would make the saved default look wrong.
      onChange(EMPTY_FILTERS);
    });
  };

  return (
    <section className="filters" aria-label="Feed filters">
      <div className="filters__head">
        {/*
          A disclosure, so a real button carrying the state: `aria-expanded`
          is the state, and the CSS rotates the chevron from that attribute
          rather than from a second prop, which is what stops the picture and
          the announcement disagreeing.
        */}
        <button
          type="button"
          className="filters__toggle"
          aria-expanded={!collapsed}
          aria-controls={bodyId}
          onClick={toggleCollapsed}
        >
          <ChevronIcon className="filters__chevron" />
          Filters
        </button>

        {/*
          In the head, which never collapses, rather than in the body with
          the chips. A search is the narrowing a reader is most likely to
          have forgotten they left on — it is a few words in a box rather
          than a chip they can see — so hiding it behind the disclosure
          would be the failure this file is written against, in its worst
          form.
        */}
        <SearchBox value={filters.query} onChange={setQuery} />

        <p className="filters__state" role="status">
          {overridden ? (
            <>
              <strong className="filters__badge">Filtered</strong>
              {effective.sources.length} of {sources.length} sources, {effective.topics.length} of{' '}
              {topics.length} topics
              {/* Named, because the counts above cannot show it: narrowing
                  to unread leaves every source and topic selected, and a
                  "Filtered" badge over "3 of 3 sources" reads as a bug. */}
              {filters.readState === 'all' ? null : `, ${filters.readState} only`}
              {/* Same reasoning as the line above, and the stronger case
                  for it: a search narrows without touching a single chip,
                  so "8 of 8 sources, 4 of 4 topics" over three results is
                  a badge actively disagreeing with the screen. */}
              {filters.query === '' ? null : `, matching “${filters.query}”`}
            </>
          ) : (
            <>
              <strong className="filters__badge filters__badge--muted">Your selection</strong>
              from Settings — {effective.sources.length} sources, {effective.topics.length} topics
            </>
          )}
        </p>

        {overridden ? (
          <div className="filters__actions">
            <button
              type="button"
              className="button button--quiet"
              onClick={saveAsDefault}
              disabled={nothingSelected || !savable}
              // Said rather than left to be inferred: a control that is
              // dead with no reason given is its own small surprise.
              title={
                nothingSelected
                  ? 'Nothing is selected, so there is no view here to make your default.'
                  : savable
                    ? 'Make the current filters your saved selection'
                    : 'Read state is not part of a saved selection — pick sources or topics to save.'
              }
            >
              Save as my default
            </button>
            <button
              type="button"
              className="button button--quiet"
              onClick={() => {
                onChange(EMPTY_FILTERS);
              }}
            >
              <CrossIcon />
              Clear filters
            </button>
          </div>
        ) : null}
      </div>

      {/*
        Collapsed and silently filtering is the failure to avoid, and the
        badge alone does not close it: "3 of 8 sources" does not say which
        three, and the chips that would are the thing that is hidden. An
        un-overridden dimension contributes nothing here, so a bar nobody
        has filtered summarises to nothing at all and keeps its "Your
        selection" badge — `null` and `[]` are different states and this is
        one of the places that has to keep them apart.
      */}
      {collapsed && summary.length > 0 ? (
        <p className="filters__summary">{summary.join('. ')}.</p>
      ) : null}

      {/*
        Two messages rather than one, because the second is a different
        claim. The first says a standing preference is narrowing this feed.
        The second says *this search cannot return anything* — every word
        the reader asked for is also a word they have muted, so the empty
        page is theirs rather than the corpus's. `role="status"` on it, and
        not on the quieter line, so a screen reader hears the one that
        explains a result rather than the one that describes a setting.
      */}
      {blocking.length > 0 ? (
        <p className="filters__muted filters__muted--blocking" role="status">
          Nothing can match: you have muted {blocking.map((term) => `“${term}”`).join(' and ')},
          which every result would contain.{' '}
          <Link to="/settings">Change what is muted</Link>.
        </p>
      ) : mutedCount > 0 ? (
        <p className="filters__muted">
          {describeMutes(preferences.muted_words.length, preferences.muted_tags.length)} hidden from
          this feed. <Link to="/settings">Change what is muted</Link>.
        </p>
      ) : null}

      {/*
        `hidden` rather than an unmounted branch. `aria-controls` has to name
        an element that exists, and the region keeps the composition panel's
        open/closed state across a collapse instead of resetting it.
      */}
      <div className="filters__body" id={bodyId} hidden={collapsed}>
        {nothingSelected ? (
          <p className="filters__note" role="status">
            Nothing is selected, so the feed is empty and there is no view to save. Pick a source or
            topic below, or clear the filters to go back to your saved selection.
          </p>
        ) : null}

        <ReadStateGroup value={filters.readState} onChange={setReadState} />

        <FilterGroup
          legend="Sources"
          entries={sources.map((source) => ({
            slug: source.slug,
            name: source.name,
            count: countBySlug.get(source.slug) ?? 0,
            highVolume: isHighVolume(source),
          }))}
          selected={effective.sources}
          onToggle={(slug) => {
            setSources(toggle(effective.sources, slug));
          }}
          onOnly={(slug) => {
            setSources([slug]);
          }}
          onAll={() => {
            setSources(sources.map((source) => source.slug));
          }}
          onNone={() => {
            setSources([]);
          }}
          showCounts={loadedCount > 0}
        />

        <FilterGroup
          legend="Topics"
          entries={topics.map((topic) => ({ slug: topic.slug, name: topic.name, count: 0, highVolume: false }))}
          selected={effective.topics}
          onToggle={(slug) => {
            setTopics(toggle(effective.topics, slug));
          }}
          onOnly={(slug) => {
            setTopics([slug]);
          }}
          onAll={() => {
            setTopics(topics.map((topic) => topic.slug));
          }}
          onNone={() => {
            setTopics([]);
          }}
          showCounts={false}
        />

        {shares.length > 1 ? (
          <details className="composition">
            <summary>
              Feed composition — {loadedCount} loaded {loadedCount === 1 ? 'item' : 'items'}
            </summary>
            <ul className="composition__list">
              {shares.map((entry) => (
                <li key={entry.slug} className="composition__row">
                  <span className="composition__name">{entry.name}</span>
                  <span className="composition__bar" style={cssVars({ '--share': entry.share })} aria-hidden="true">
                    <span className="composition__fill" />
                  </span>
                  <span className="composition__count">
                    {entry.count} · {percent(entry.share)}
                  </span>
                  <button
                    type="button"
                    className="button button--tiny"
                    onClick={() => {
                      setSources([entry.slug]);
                    }}
                  >
                    Only
                  </button>
                  <button
                    type="button"
                    className="button button--tiny"
                    onClick={() => {
                      setSources(effective.sources.filter((slug) => slug !== entry.slug));
                    }}
                  >
                    Hide
                  </button>
                </li>
              ))}
            </ul>
          </details>
        ) : null}
      </div>
    </section>
  );
}

const READ_STATE_CHOICES: { value: ReadFilter; label: string; hint: string }[] = [
  { value: 'all', label: 'All', hint: 'Every item, read or not' },
  { value: 'unread', label: 'Unread', hint: 'Only items you have not opened' },
  { value: 'read', label: 'Read', hint: 'Only items you have already opened' },
];

/**
 * One choice of three rather than a set of toggles, so no All/None bulk
 * controls and no empty state to reach: `all` *is* the way back. It is
 * also the one filter here that cannot be saved as a default — see the
 * guard on "Save as my default" — so it carries no per-chip affordances
 * that would imply otherwise.
 */
function ReadStateGroup({
  value,
  onChange,
}: {
  value: ReadFilter;
  onChange: (next: ReadFilter) => void;
}) {
  const groupId = 'filter-group-read-state';
  return (
    <div className="filter-group">
      <div className="filter-group__head">
        <h2 className="filter-group__legend" id={groupId}>
          Status
        </h2>
      </div>
      <ul className="chips" aria-labelledby={groupId}>
        {READ_STATE_CHOICES.map((choice) => (
          <li key={choice.value}>
            <button
              type="button"
              className="chip"
              aria-pressed={value === choice.value}
              title={choice.hint}
              onClick={() => {
                onChange(choice.value);
              }}
            >
              <span className="chip__label">{choice.label}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

interface FilterEntry {
  slug: string;
  name: string;
  count: number;
  highVolume: boolean;
}

interface FilterGroupProps {
  legend: string;
  entries: FilterEntry[];
  selected: string[];
  onToggle: (slug: string) => void;
  onOnly: (slug: string) => void;
  onAll: () => void;
  onNone: () => void;
  showCounts: boolean;
}

function FilterGroup({
  legend,
  entries,
  selected,
  onToggle,
  onOnly,
  onAll,
  onNone,
  showCounts,
}: FilterGroupProps) {
  const active = new Set(selected);
  const groupId = `filter-group-${legend.toLowerCase()}`;

  return (
    <div className="filter-group">
      <div className="filter-group__head">
        <h2 className="filter-group__legend" id={groupId}>
          {legend}
        </h2>
        <span className="filter-group__bulk">
          <button type="button" className="button button--tiny" onClick={onAll}>
            All
          </button>
          <button type="button" className="button button--tiny" onClick={onNone}>
            None
          </button>
        </span>
      </div>

      <ul className="chips" aria-labelledby={groupId}>
        {entries.map((entry) => {
          const on = active.has(entry.slug);
          return (
            <li key={entry.slug}>
              <button
                type="button"
                className="chip"
                aria-pressed={on}
                onClick={() => {
                  onToggle(entry.slug);
                }}
                onDoubleClick={() => {
                  onOnly(entry.slug);
                }}
                title={
                  entry.highVolume
                    ? `${entry.name} publishes frequently and can dominate a time-ordered feed. Double-click to show only this one.`
                    : `Double-click to show only ${entry.name}`
                }
              >
                <span className="chip__label">{entry.name}</span>
                {entry.highVolume ? (
                  <span className="chip__flag" title="High volume">
                    <span aria-hidden="true">▲</span>
                    <span className="visually-hidden">high volume</span>
                  </span>
                ) : null}
                {showCounts && entry.count > 0 ? <span className="chip__count">{entry.count}</span> : null}
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}


function describeMutes(words: number, tags: number): string {
  // Counts rather than the terms themselves. The terms are the reader's
  // own words and some of them are muted precisely because the reader does
  // not want to read them — printing them back across the top of the feed
  // would defeat the setting it describes.
  const parts: string[] = [];
  if (words > 0) parts.push(`${String(words)} ${words === 1 ? 'word' : 'words'}`);
  if (tags > 0) parts.push(`${String(tags)} ${tags === 1 ? 'topic' : 'topics'}`);
  return parts.join(' and ');
}
