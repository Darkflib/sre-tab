import type { PreferencesPatch } from '../api/types';
import { useCatalogue } from '../catalogue/useCatalogue';
import {
  effectiveSelection,
  type FeedFilters,
  hasOverride,
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

  // Nothing selected is a step, not a destination: it exists so you can
  // clear the chips and pick two, rather than deselecting sixteen. It also
  // cannot be saved — an empty saved selection is how the server spells
  // "no preference, use the instance defaults", so storing it would mean
  // the opposite of what the button says.
  const nothingSelected = selectsNothing(filters);

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
      onChange({ topics: null, sources: null });
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
              disabled={nothingSelected}
              // Said rather than left to be inferred: a control that is
              // dead with no reason given is its own small surprise.
              title={
                nothingSelected
                  ? 'Nothing is selected, so there is no view here to make your default.'
                  : 'Make the current filters your saved selection'
              }
            >
              Save as my default
            </button>
            <button
              type="button"
              className="button button--quiet"
              onClick={() => {
                onChange({ topics: null, sources: null });
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
