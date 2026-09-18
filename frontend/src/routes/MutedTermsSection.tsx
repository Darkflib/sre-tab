import { useState } from 'react';

import type { PreferencesPatch, Preferences, Topic } from '../api/types';
import { CrossIcon } from '../components/icons';

/** Matches `MAX_MUTED_TERM_LENGTH` in `app/db/models.py`, which is also the
 *  column width — so the input stops where the server's 422 begins. */
export const MAX_TERM_LENGTH = 64;

/** Matches `MAX_MUTED_TERMS` in `app/api/v1/schemas/me.py`. */
export const MAX_TERMS = 100;

/** Matches `MAX_URL_LENGTH` in `app/ingest/normalise.py`: the bound on what
 *  may be *pasted* as a URL mute, which is not the bound on what is stored.
 *  Capping the input at `MAX_TERM_LENGTH` would refuse an ordinary article
 *  link before the reduction that makes it fit had run. */
export const MAX_URL_INPUT_LENGTH = 2048;

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
 * Words, tags, and sites are three lists, not one with a type selector,
 * because they behave differently in the way that matters to a reader: a
 * word is anything they can type, a tag has to be one the catalogue knows,
 * and a site is reduced to its host and first path segment before it is
 * kept. Three controls make each constraint obvious without an error
 * message.
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

      <UrlList
        urls={preferences.muted_urls}
        onChange={(muted_urls) => {
          onSave({ muted_urls });
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

  // The same cap `WordList` carries, and it was missing here: at a hundred
  // muted topics, checking one more sent a hundred and one, the API
  // answered 422, and the reader got a failed save and a checkbox that
  // sprang back — for a limit this screen had never mentioned. Only the
  // *unchecked* controls go dead, so the way back out stays open; a cap
  // that also blocks removal is a trap rather than a limit.
  const full = muted.length >= MAX_TERMS;

  const toggleTag = (slug: string) => {
    if (full && !muted.includes(slug)) return;
    onChange(muted.includes(slug) ? muted.filter((entry) => entry !== slug) : [...muted, slug]);
  };

  return (
    <fieldset className="settings__field">
      <legend>Topics</legend>
      <p className="settings__hint">
        Topics come from the source, and a publisher&rsquo;s section in the article&rsquo;s link
        can add more — so muting <em>sport</em> hides the football, and muting a topic a source
        carries hides everything it publishes.
      </p>
      {full ? (
        <p className="settings__hint" id="mute-tag-problem" role="status">
          That is {MAX_TERMS} muted topics, which is the limit. Remove one to add another.
        </p>
      ) : null}
      <ul className="option-grid">
        {rows.map((topic) => (
          <li key={topic.slug}>
            <label className="option">
              <input
                type="checkbox"
                checked={muted.includes(topic.slug)}
                disabled={full && !muted.includes(topic.slug)}
                aria-describedby={full ? 'mute-tag-problem' : undefined}
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

/**
 * What the server will keep for a pasted link, as near as the browser can
 * tell: the host without its `www.`, and the first path segment.
 *
 * A preview, not the reduction. `app.services.preferences.url_mute_term`
 * reduces again and its answer is what is stored and what comes back in the
 * list, so the reader's own input is what gets sent — a difference between
 * the browser's URL parser and the server's then costs a preview that was
 * slightly off, never a mute of something the reader did not paste. What
 * this is for is the moment before saving: a whole article link means its
 * author or its section, and the reader should see that before it happens,
 * and a second post by an author already muted should read as a duplicate
 * rather than as a save that changes nothing.
 *
 * `null` for anything that is not an http(s) link with a dotted host, or
 * that the server would refuse for a reason visible here (a port,
 * credentials, an empty first segment, a host that is `www.` twice over),
 * which leaves the button dead.
 */
export function urlMuteTerm(input: string): string | null {
  const trimmed = input.trim();
  if (trimmed === '') return null;
  const candidate = /^[a-z][a-z0-9+.-]*:\/\//i.test(trimmed)
    ? trimmed
    : `https://${trimmed.replace(/^\/\//, '')}`;
  let url: URL;
  try {
    url = new URL(candidate);
  } catch {
    return null;
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
  if (url.username !== '' || url.password !== '' || url.port !== '') return null;
  const host = url.hostname.replace(/\.$/, '').replace(/^www\./, '');
  // The server's rule, not a lookalike: one `www.` goes, and a host still
  // starting with one is refused, because storing it would not survive
  // the next save's re-reduction.
  if (host.startsWith('www.') || !host.includes('.')) return null;
  const segment = url.pathname.split('/')[1] ?? '';
  if (segment === '' && url.pathname !== '/') return null;
  return (segment === '' ? host : `${host}/${segment}`).toLowerCase();
}

function UrlList({ urls, onChange }: { urls: string[]; onChange: (next: string[]) => void }) {
  const [draft, setDraft] = useState('');
  const term = urlMuteTerm(draft);
  const duplicate = term !== null && urls.includes(term);
  // Refused here for the reason the server refuses it: a shortened segment
  // is a different segment, and could be somebody else's.
  const tooLong = term !== null && term.length > MAX_TERM_LENGTH;
  const full = urls.length >= MAX_TERMS;
  const blocked = term === null || duplicate || tooLong || full;

  const add = () => {
    if (blocked) return;
    onChange([...urls, draft.trim()]);
    setDraft('');
  };

  let problem: string | null = null;
  if (duplicate) problem = `“${term}” is already muted.`;
  else if (tooLong)
    problem = `That comes to “${term.slice(0, 32)}…”, which is longer than the ${String(MAX_TERM_LENGTH)} characters a mute can hold.`;
  else if (full && draft.trim() !== '')
    problem = `That is ${String(MAX_TERMS)} muted sites, which is the limit. Remove one to add another.`;

  return (
    <fieldset className="settings__field">
      <legend>Sites and authors</legend>
      <p className="settings__hint">
        Paste a link or type a site. A link mutes the first part of its path — the author on{' '}
        <code>dev.to</code>, the section on the Guardian — and a bare site such as{' '}
        <code>medium.com</code> is muted inside Hacker News and Lobsters too. A subdomain like{' '}
        <code>alice.medium.com</code> is a site of its own.
      </p>

      <form
        className="muted__add"
        onSubmit={(event) => {
          event.preventDefault();
          add();
        }}
      >
        <label className="visually-hidden" htmlFor="mute-url">
          A link or site to mute
        </label>
        <input
          id="mute-url"
          className="input muted__input"
          type="text"
          inputMode="url"
          value={draft}
          maxLength={MAX_URL_INPUT_LENGTH}
          placeholder="dev.to/someone"
          aria-describedby={problem ? 'mute-url-problem' : term ? 'mute-url-preview' : undefined}
          onChange={(event) => {
            setDraft(event.target.value);
          }}
        />
        <button type="submit" className="button" disabled={blocked}>
          Mute
        </button>
      </form>

      {problem ? (
        <p className="settings__hint" id="mute-url-problem" role="status">
          {problem}
        </p>
      ) : term ? (
        <p className="settings__hint" id="mute-url-preview">
          Mutes everything under <code>{term}</code>.
        </p>
      ) : null}

      <TermList
        terms={urls}
        empty="Nothing is muted by site."
        label={(entry) => `Stop muting ${entry}`}
        onRemove={(entry) => {
          onChange(urls.filter((existing) => existing !== entry));
        }}
      />
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
