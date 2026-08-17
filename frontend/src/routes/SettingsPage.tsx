import { useEffect, useRef, useState } from 'react';

import { ApiError } from '../api/client';
import type { Layout, PreferencesPatch, Theme } from '../api/types';
import { ErrorState, LoadingState, Spinner } from '../components/States';
import { useCatalogue } from '../catalogue/useCatalogue';
import { toggle } from '../feed/filters';
import { HIGH_VOLUME_REFRESH_MINUTES, isHighVolume } from '../feed/volume';
import { useAuthenticatedSession } from '../session/useSession';

type SaveState = 'idle' | 'saving' | 'saved' | 'error';

const THEMES: { value: Theme; label: string; hint: string }[] = [
  { value: 'light', label: 'Light', hint: 'Always light' },
  { value: 'dark', label: 'Dark', hint: 'Always dark' },
  { value: 'system', label: 'System', hint: 'Follow the operating system' },
];

const LAYOUTS: { value: Layout; label: string; hint: string }[] = [
  { value: 'grid', label: 'Grid', hint: 'Cards side by side' },
  { value: 'list', label: 'List', hint: 'One item per row' },
];

export function SettingsPage() {
  const { user, preferences, updatePreferences, signOut, removeAccount } = useAuthenticatedSession();
  const catalogue = useCatalogue();

  const [saveState, setSaveState] = useState<SaveState>('idle');
  const [saveError, setSaveError] = useState<ApiError | null>(null);
  const savedTimer = useRef<number | undefined>(undefined);

  useEffect(
    () => () => {
      window.clearTimeout(savedTimer.current);
    },
    [],
  );

  const save = (patch: PreferencesPatch) => {
    setSaveState('saving');
    setSaveError(null);
    updatePreferences(patch)
      .then(() => {
        setSaveState('saved');
        window.clearTimeout(savedTimer.current);
        savedTimer.current = window.setTimeout(() => {
          setSaveState('idle');
        }, 2000);
      })
      .catch((cause: unknown) => {
        setSaveError(cause instanceof ApiError ? cause : new ApiError(0, 'Unexpected error.'));
        setSaveState('error');
      });
  };

  return (
    <div className="settings">
      <div className="settings__head">
        <h1>Settings</h1>
        <p className="settings__status" role="status">
          {saveState === 'saving' ? (
            <>
              <Spinner label="Saving" /> Saving…
            </>
          ) : null}
          {saveState === 'saved' ? 'Saved' : null}
          {saveState === 'error' && saveError ? (
            <span className="settings__status-error">{saveError.message}</span>
          ) : null}
        </p>
      </div>

      <p className="settings__lede">
        Everything here is stored on the server against your account, so it follows you to another
        browser. Changes save as you make them.
      </p>

      <section className="settings__section" aria-labelledby="settings-appearance">
        <h2 id="settings-appearance">Appearance</h2>

        <fieldset className="settings__field">
          <legend>Theme</legend>
          <div className="radio-row">
            {THEMES.map((option) => (
              <label key={option.value} className="option option--radio">
                <input
                  type="radio"
                  name="theme"
                  value={option.value}
                  checked={preferences.theme === option.value}
                  onChange={() => {
                    save({ theme: option.value });
                  }}
                />
                <span className="option__label">{option.label}</span>
                <span className="option__hint">{option.hint}</span>
              </label>
            ))}
          </div>
        </fieldset>

        <fieldset className="settings__field">
          <legend>Layout</legend>
          <div className="radio-row">
            {LAYOUTS.map((option) => (
              <label key={option.value} className="option option--radio">
                <input
                  type="radio"
                  name="layout"
                  value={option.value}
                  checked={preferences.layout === option.value}
                  onChange={() => {
                    save({ layout: option.value });
                  }}
                />
                <span className="option__label">{option.label}</span>
                <span className="option__hint">{option.hint}</span>
              </label>
            ))}
          </div>
        </fieldset>

        <MaxCardsField
          value={preferences.max_visible_cards}
          onCommit={(value) => {
            save({ max_visible_cards: value });
          }}
        />
      </section>

      <section className="settings__section" aria-labelledby="settings-selection">
        <h2 id="settings-selection">Topics and sources</h2>
        <p className="settings__hint">
          These are your defaults. The feed can be filtered without touching them.
        </p>

        {catalogue.status === 'loading' ? <LoadingState label="Loading the catalogue" /> : null}
        {catalogue.status === 'error' && catalogue.error ? (
          <ErrorState error={catalogue.error} onRetry={catalogue.reload} what="the catalogue" />
        ) : null}

        {catalogue.status === 'ready' ? (
          <>
            <fieldset className="settings__field">
              <legend>Topics</legend>
              <ul className="option-grid">
                {catalogue.topics
                  .filter((topic) => topic.enabled)
                  .map((topic) => (
                    <li key={topic.slug}>
                      <label className="option">
                        <input
                          type="checkbox"
                          checked={preferences.topics.includes(topic.slug)}
                          onChange={() => {
                            save({ topics: toggle(preferences.topics, topic.slug) });
                          }}
                        />
                        <span className="option__label">{topic.name}</span>
                      </label>
                    </li>
                  ))}
              </ul>
            </fieldset>

            <fieldset className="settings__field">
              <legend>Sources</legend>
              <p className="settings__hint">
                <span className="chip__flag chip__flag--inline">▲ high volume</span> marks sources that
                refresh every {HIGH_VOLUME_REFRESH_MINUTES} minutes or faster. Enabling several of them will crowd out the slower
                ones in a time-ordered feed.
              </p>
              <ul className="option-grid">
                {catalogue.sources
                  .filter((source) => source.enabled)
                  .map((source) => (
                    <li key={source.slug}>
                      <label className="option">
                        <input
                          type="checkbox"
                          checked={preferences.sources.includes(source.slug)}
                          onChange={() => {
                            save({ sources: toggle(preferences.sources, source.slug) });
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
                        <span className="option__hint">every {source.refresh_minutes} min</span>
                      </label>
                    </li>
                  ))}
              </ul>
            </fieldset>
          </>
        ) : null}
      </section>

      <AccountSection
        login={user.github_login}
        displayName={user.display_name}
        avatarUrl={user.avatar_url}
        onSignOut={signOut}
        onDelete={removeAccount}
      />
    </div>
  );
}

function MaxCardsField({
  value,
  onCommit,
}: {
  value: number;
  onCommit: (next: number) => void;
}) {
  const [draft, setDraft] = useState(String(value));
  const [lastValue, setLastValue] = useState(value);
  const timer = useRef<number | undefined>(undefined);

  // Adjust during render rather than in an effect: the server's value
  // wins as soon as it lands, without a second paint of the stale draft.
  if (lastValue !== value) {
    setLastValue(value);
    setDraft(String(value));
  }

  useEffect(
    () => () => {
      window.clearTimeout(timer.current);
    },
    [],
  );

  const commit = (raw: string) => {
    const parsed = Number.parseInt(raw, 10);
    if (Number.isNaN(parsed)) return;
    const bounded = Math.min(100, Math.max(1, parsed));
    if (bounded !== value) onCommit(bounded);
  };

  return (
    <div className="settings__field">
      <label className="settings__label" htmlFor="max-cards">
        Items per page
      </label>
      <input
        id="max-cards"
        className="input input--number"
        type="number"
        min={1}
        max={100}
        step={1}
        value={draft}
        aria-describedby="max-cards-hint"
        onChange={(event) => {
          setDraft(event.target.value);
          window.clearTimeout(timer.current);
          const raw = event.target.value;
          timer.current = window.setTimeout(() => {
            commit(raw);
          }, 600);
        }}
        onBlur={(event) => {
          window.clearTimeout(timer.current);
          commit(event.target.value);
        }}
      />
      <p id="max-cards-hint" className="settings__hint">
        How many items the feed loads at a time, between 1 and 100.
      </p>
    </div>
  );
}

function AccountSection({
  login,
  displayName,
  avatarUrl,
  onSignOut,
  onDelete,
}: {
  login: string;
  displayName: string | null;
  avatarUrl: string | null;
  onSignOut: () => Promise<void>;
  onDelete: () => Promise<void>;
}) {
  const [confirming, setConfirming] = useState(false);
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  const run = (work: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    work()
      .catch((cause: unknown) => {
        setError(cause instanceof ApiError ? cause : new ApiError(0, 'Unexpected error.'));
      })
      .finally(() => {
        setBusy(false);
      });
  };

  return (
    <section className="settings__section" aria-labelledby="settings-account">
      <h2 id="settings-account">Account</h2>

      <p className="settings__account">
        {avatarUrl ? (
          <img
            className="settings__avatar"
            src={avatarUrl}
            alt=""
            width={40}
            height={40}
            referrerPolicy="no-referrer"
          />
        ) : null}
        <span>
          <strong>{displayName || login}</strong>
          <br />
          <span className="settings__hint">GitHub: {login}</span>
        </span>
      </p>

      {error ? (
        <p className="banner banner--error" role="alert">
          {error.message}
        </p>
      ) : null}

      <div className="settings__account-actions">
        <button
          type="button"
          className="button"
          disabled={busy}
          onClick={() => {
            run(onSignOut);
          }}
        >
          Sign out
        </button>

        {confirming ? null : (
          <button
            type="button"
            className="button button--danger"
            onClick={() => {
              setConfirming(true);
            }}
          >
            Delete my account
          </button>
        )}
      </div>

      {confirming ? (
        <div className="danger-zone">
          <h3>Delete your account</h3>
          <p>
            This removes your profile, preferences, bookmarks, read state, and every session. Feed items
            themselves belong to the instance and are unaffected. It cannot be undone.
          </p>
          <label className="settings__label" htmlFor="confirm-login">
            Type <code>{login}</code> to confirm
          </label>
          <input
            id="confirm-login"
            className="input"
            type="text"
            value={typed}
            autoComplete="off"
            onChange={(event) => {
              setTyped(event.target.value);
            }}
          />
          <div className="settings__account-actions">
            <button
              type="button"
              className="button button--danger"
              disabled={typed !== login || busy}
              onClick={() => {
                run(onDelete);
              }}
            >
              Delete permanently
            </button>
            <button
              type="button"
              className="button button--quiet"
              onClick={() => {
                setConfirming(false);
                setTyped('');
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : null}
    </section>
  );
}
