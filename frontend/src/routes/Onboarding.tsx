import { useMemo, useState, type SyntheticEvent } from 'react';
import { useNavigate } from 'react-router-dom';

import { ApiError } from '../api/client';
import { ErrorState, LoadingState, Spinner } from '../components/States';
import { useCatalogue } from '../catalogue/useCatalogue';
import { toggle } from '../feed/filters';
import { isHighVolume } from '../feed/volume';
import { useAuthenticatedSession } from '../session/useSession';

export function Onboarding() {
  const navigate = useNavigate();
  const { preferences, updatePreferences } = useAuthenticatedSession();
  const catalogue = useCatalogue();

  const enabledTopics = useMemo(
    () => catalogue.topics.filter((topic) => topic.enabled),
    [catalogue.topics],
  );
  const enabledSources = useMemo(
    () => catalogue.sources.filter((source) => source.enabled),
    [catalogue.sources],
  );

  const [topics, setTopics] = useState<string[] | null>(null);
  const [sources, setSources] = useState<string[] | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  if (catalogue.status === 'loading') {
    return (
      <div className="onboarding">
        <LoadingState label="Loading the source catalogue" />
      </div>
    );
  }

  if (catalogue.status === 'error' && catalogue.error) {
    return (
      <div className="onboarding">
        <ErrorState error={catalogue.error} onRetry={catalogue.reload} what="the source catalogue" />
      </div>
    );
  }

  // Defaults, deliberately not "everything": the high-cadence sources are
  // left off so a first feed is readable rather than a news ticker.
  const defaultTopics =
    preferences.topics.length > 0 ? preferences.topics : enabledTopics.map((topic) => topic.slug);
  const defaultSources =
    preferences.sources.length > 0
      ? preferences.sources
      : enabledSources.filter((source) => !isHighVolume(source)).map((source) => source.slug);

  const selectedTopics = topics ?? defaultTopics;
  const selectedSources = sources ?? defaultSources;

  const submit = (event: SyntheticEvent) => {
    event.preventDefault();
    setSaving(true);
    setError(null);
    updatePreferences({
      topics: selectedTopics,
      sources: selectedSources,
      onboarding_completed: true,
    })
      .then(() => navigate('/feed', { replace: true }))
      .catch((cause: unknown) => {
        setError(cause instanceof ApiError ? cause : new ApiError(0, 'Unexpected error.'));
        setSaving(false);
      });
  };

  return (
    <div className="onboarding">
      <main id="main" className="onboarding__panel" tabIndex={-1}>
        <h1>Set up your feed</h1>
        <p className="onboarding__lede">
          Choose what you want to see. You can change any of this later in Settings, and filter the feed
          without changing your defaults.
        </p>

        <form onSubmit={submit}>
          <fieldset className="onboarding__group">
            <legend>Topics</legend>
            <p className="onboarding__hint">Items are tagged by the operator when a source is configured.</p>
            <ul className="option-grid">
              {enabledTopics.map((topic) => (
                <li key={topic.slug}>
                  <label className="option">
                    <input
                      type="checkbox"
                      checked={selectedTopics.includes(topic.slug)}
                      onChange={() => {
                        setTopics(toggle(selectedTopics, topic.slug));
                      }}
                    />
                    <span className="option__label">{topic.name}</span>
                  </label>
                </li>
              ))}
            </ul>
          </fieldset>

          <fieldset className="onboarding__group">
            <legend>Sources</legend>
            <p className="onboarding__hint">
              Sources marked <span className="chip__flag chip__flag--inline">▲ high volume</span> publish
              far more often than the rest. They are switched off to begin with because a feed ordered by
              time otherwise belongs to them within the hour — turn them on once you have a feel for it.
            </p>
            <ul className="option-grid">
              {enabledSources.map((source) => (
                <li key={source.slug}>
                  <label className="option">
                    <input
                      type="checkbox"
                      checked={selectedSources.includes(source.slug)}
                      onChange={() => {
                        setSources(toggle(selectedSources, source.slug));
                      }}
                    />
                    <span className="option__label">
                      {source.name}
                      {isHighVolume(source) ? (
                        <span className="chip__flag" title="High volume">
                          <span aria-hidden="true">▲</span>
                          <span className="visually-hidden">high volume</span>
                        </span>
                      ) : null}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          </fieldset>

          {error ? (
            <p className="form__error" role="alert">
              {error.message}
            </p>
          ) : null}

          <div className="onboarding__actions">
            <button type="submit" className="button button--primary button--large" disabled={saving}>
              {saving ? <Spinner label="Saving" /> : null}
              Start reading
            </button>
          </div>
        </form>
      </main>
    </div>
  );
}
