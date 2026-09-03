import { useState } from 'react';

import type { PreferencesPatch, Preferences, Topic } from '../api/types';
import { CrossIcon } from '../components/icons';

/** Matches `MAX_MUTED_TERM_LENGTH` in `app/db/models.py`, which is also the
 *  column width — so the input stops where the server's 422 begins. */
export const MAX_TERM_LENGTH = 64;

/** Matches `MAX_MUTED_TERMS` in `app/api/v1/schemas/me.py`. */
export const MAX_TERMS = 100;

interface MutedTermsSectionProps {
  preferences: Preferences;
  topics: Topic[];
  onSave: (patch: PreferencesPatch) => void;
}

/**
 * Muted words and tags, and the one thing this screen has to get right:
 * **muting is the only filter with no evidence of itself on the feed.**
 *
 * Every other narrowing is visible where it acts — a deselected source is a
 * chip you can see, a search is text in a box above the results. A mute
 * removes items with nothing left behind, so the list of what is muted has
 * to be somewhere the reader will find it when the feed looks wrong, and
 * every entry has to come off in one click. Hence a plain list with a
 * remove control on each row rather than a textarea of comma-separated
 * terms, which would be fewer components and would make "why am I not
 * seeing anything about Rust?" an editing exercise.
 *
 * Words and tags are two lists, not one with a type selector, because they
 * behave differently in the way that matters to a reader: a word is
 * anything they can type and a tag has to be one the catalogue knows. Two
 * controls make the second constraint obvious without an error message.
 */
export function MutedTermsSection({ preferences, topics, onSave }: MutedTermsSectionProps) {
  return (
    <section className="settings__section" aria-labelledby="settings-muted">
      <h2 id="settings-muted">Muted</h2>
      <p className="settings__hint">
        Anything muted here is removed from the feed everywhere — it is not a filter you can see
        working, so this list is the only place it shows. Bookmarks are never muted.
      </p>

      <WordList
        words={preferences.muted_words}
        onChange={(muted_words) => {
          onSave({ muted_words });
        }}
      />

      <TagList
        muted={preferences.muted_tags}
        topics={topics}
        onChange={(muted_tags) => {
          onSave({ muted_tags });
        }}
      />
    </section>
  );
}

function WordList({ words, onChange }: { words: string[]; onChange: (next: string[]) => void }) {
  const [draft, setDraft] = useState('');
  const normalised = draft.trim().replace(/\s+/g, ' ').toLowerCase();
  // The server normalises and deduplicates too — this is so the button is
  // dead rather than the save being a no-op the reader cannot explain.
  const duplicate = normalised !== '' && words.includes(normalised);
  const full = words.length >= MAX_TERMS;

  const add = () => {
    if (normalised === '' || duplicate || full) return;
    onChange([...words, normalised]);
    setDraft('');
  };

  return (
    <fieldset className="settings__field">
      <legend>Words and phrases</legend>
      <p className="settings__hint">
        Matched against each item&rsquo;s title and summary. A phrase needs all of its words, so
        muting <code>premier league</code> hides the league and not every mention of a premier.
      </p>

      <form
        className="muted__add"
        onSubmit={(event) => {
          event.preventDefault();
          add();
        }}
      >
        <label className="visually-hidden" htmlFor="mute-word">
          A word or phrase to mute
        </label>
        <input
          id="mute-word"
          className="input muted__input"
          type="text"
          value={draft}
          maxLength={MAX_TERM_LENGTH}
          placeholder="football"
          aria-describedby={duplicate || full ? 'mute-word-problem' : undefined}
          onChange={(event) => {
            setDraft(event.target.value);
          }}
        />
        <button type="submit" className="button" disabled={normalised === '' || duplicate || full}>
          Mute
        </button>
      </form>

      {duplicate || full ? (
        <p className="settings__hint" id="mute-word-problem" role="status">
          {duplicate
            ? `“${normalised}” is already muted.`
            : `That is ${String(MAX_TERMS)} muted words, which is the limit. Remove one to add another.`}
        </p>
      ) : null}

      <TermList
        terms={words}
        empty="Nothing is muted by word."
        label={(term) => `Stop muting ${term}`}
        onRemove={(term) => {
          onChange(words.filter((entry) => entry !== term));
        }}
      />
    </fieldset>
  );
}

function TagList({
  muted,
  topics,
  onChange,
}: {
  muted: string[];
  topics: Topic[];
  onChange: (next: string[]) => void;
}) {
  // Checkboxes rather than the add-a-term form above, because the
  // vocabulary is closed: every mutable tag is already on screen, so there
  // is nothing to type and nothing to get wrong.
  //
  // "Every" has to include the retired ones, and that is a fix rather than
  // a nicety. The catalogue returns only enabled topics; the feed's mute
  // predicate matches slugs and never consults `topics.enabled`. So an
  // operator disabling a topic somebody had muted left the mute working
  // and its checkbox gone — a setting still hiding items with no control
  // anywhere to turn it off. Anything muted is listed whether the
  // catalogue still knows it or not, named by its slug when that is all
  // there is left of it.
  const known = new Set(topics.map((topic) => topic.slug));
  const rows = [
    ...topics.filter((topic) => topic.enabled).map(({ slug, name }) => ({ slug, name })),
    ...muted.filter((slug) => !known.has(slug)).map((slug) => ({ slug, name: slug })),
  ];

  const toggleTag = (slug: string) => {
    onChange(
      muted.includes(slug) ? muted.filter((entry) => entry !== slug) : [...muted, slug],
    );
  };

  return (
    <fieldset className="settings__field">
      <legend>Topics</legend>
      <p className="settings__hint">
        Topics come from the source rather than from the article, so muting one hides everything
        that source publishes under it. Muting words is usually the narrower tool.
      </p>
      <ul className="option-grid">
        {rows.map((topic) => (
          <li key={topic.slug}>
            <label className="option">
              <input
                type="checkbox"
                checked={muted.includes(topic.slug)}
                onChange={() => {
                  toggleTag(topic.slug);
                }}
              />
              <span className="option__label">{topic.name}</span>
              {known.has(topic.slug) ? null : (
                <span className="option__hint" title="No longer in the catalogue">
                  retired
                </span>
              )}
            </label>
          </li>
        ))}
      </ul>
    </fieldset>
  );
}

function TermList({
  terms,
  empty,
  label,
  onRemove,
}: {
  terms: string[];
  empty: string;
  label: (term: string) => string;
  onRemove: (term: string) => void;
}) {
  if (terms.length === 0) return <p className="settings__hint">{empty}</p>;
  return (
    <ul className="muted__list">
      {terms.map((term) => (
        <li key={term} className="muted__term">
          <span>{term}</span>
          <button
            type="button"
            className="muted__remove"
            onClick={() => {
              onRemove(term);
            }}
          >
            <CrossIcon />
            <span className="visually-hidden">{label(term)}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}
