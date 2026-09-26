import { useState } from 'react';

import type { Preferences, PreferencesPatch } from '../api/types';
import { CrossIcon } from '../components/icons';
import { LANGUAGE_CODES, languageName, preferredLanguage } from '../lib/languages';

/** Matches `MAX_LANGUAGES` in `app/api/v1/schemas/me.py`. */
export const MAX_LANGUAGES = 32;

interface LanguagesSectionProps {
  preferences: Preferences;
  onSave: (patch: PreferencesPatch) => void;
}

/**
 * The languages a reader reads. An allow-list, because the question is
 * "only these" — nobody wants to mute Portuguese, then Spanish, then Thai,
 * one arrival at a time.
 *
 * A picker rather than a free-text box, because the vocabulary is closed and
 * a code the server's detector never emits would be refused, and the one
 * word a reader is most likely to type — "English" — is not a code.
 *
 * The hint carries the rule that makes the setting safe to turn on: an
 * item whose language could not be told is always shown. Without it, the
 * reader who sees a two-word English title in an "English only" feed has
 * no way to know that is by design.
 */
export function LanguagesSection({ preferences, onSave }: LanguagesSectionProps) {
  const chosen = preferences.languages;
  const options = LANGUAGE_CODES.filter((code) => !chosen.includes(code))
    .map((code) => ({ code, name: languageName(code) }))
    .sort((a, b) => a.name.localeCompare(b.name));

  const [draft, setDraft] = useState(() => preferredLanguage());
  // The draft may be one just added; fall back to the first on offer
  // rather than leaving the select showing a value it no longer holds.
  const selected = options.some((option) => option.code === draft)
    ? draft
    : (options[0]?.code ?? '');
  const full = chosen.length >= MAX_LANGUAGES;

  const add = () => {
    if (selected === '' || full) return;
    onSave({ languages: [...chosen, selected] });
  };

  return (
    <section className="settings__section" aria-labelledby="settings-languages">
      <h2 id="settings-languages">Languages</h2>
      <p className="settings__hint">
        Show only items written in these languages. The language is detected from each
        item&rsquo;s title and summary, and an item it cannot be sure about is always shown, so a
        short headline is never hidden by a guess. Leave the list empty to see every language.
        Bookmarks are never filtered.
      </p>

      <form
        className="muted__add"
        onSubmit={(event) => {
          event.preventDefault();
          add();
        }}
      >
        <label className="visually-hidden" htmlFor="language-add">
          A language to add
        </label>
        <select
          id="language-add"
          className="input muted__input"
          value={selected}
          disabled={full}
          onChange={(event) => {
            setDraft(event.target.value);
          }}
        >
          {options.map(({ code, name }) => (
            <option key={code} value={code}>
              {name} ({code})
            </option>
          ))}
        </select>
        <button type="submit" className="button" disabled={selected === '' || full}>
          Add
        </button>
      </form>

      {full ? (
        <p className="settings__hint" role="status">
          That is {MAX_LANGUAGES} languages, which is the limit. Remove one to add another.
        </p>
      ) : null}

      {chosen.length === 0 ? (
        <p className="settings__hint">Every language is shown.</p>
      ) : (
        <ul className="muted__list">
          {chosen.map((code) => (
            <li key={code} className="muted__term">
              <span>{languageName(code)}</span>
              <button
                type="button"
                className="muted__remove"
                onClick={() => {
                  onSave({ languages: chosen.filter((entry) => entry !== code) });
                }}
              >
                <CrossIcon />
                <span className="visually-hidden">Stop showing only {languageName(code)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
