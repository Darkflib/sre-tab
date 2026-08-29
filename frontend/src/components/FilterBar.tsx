import type { PreferencesPatch, ReadFilter } from '../api/types';
import { useCatalogue } from '../catalogue/useCatalogue';
import {
  effectiveSelection,
  EMPTY_FILTERS,
  type FeedFilters,
  hasOverride,
  hasSavableOverride,
  selectsNothing,
  toggle,
} from '../feed/filters';
import { isHighVolume, type SourceShare } from '../feed/volume';
import { cssVars } from '../lib/css';
import { percent } from '../lib/format';
import { useAuthenticatedSession } from '../session/useSession';
import { CrossIcon } from './icons';

interface FilterBarProps {
  filters: FeedFilters;
  onChange: (next: FeedFilters) => void;
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
 */
export function FilterBar({ filters, onChange, shares, loadedCount }: FilterBarProps) {
  const { sources, topics } = useCatalogue();
  const { preferences, updatePreferences } = useAuthenticatedSession();

  const effective = effectiveSelection(filters, preferences, { sources, topics });
  const overridden = hasOverride(filters);
  const countBySlug = new Map(shares.map((entry) => [entry.slug, entry.count]));

  const setSources = (next: string[] | null) => {
    onChange({ ...filters, sources: next });
  };
  const setTopics = (next: string[] | null) => {
    onChange({ ...filters, topics: next });
  };
  const setReadState = (next: ReadFilter) => {
    onChange({ ...filters, readState: next });
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
