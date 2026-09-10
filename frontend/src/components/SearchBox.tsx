import { useEffect, useId, useRef, useState } from 'react';

import { MAX_QUERY_LENGTH } from '../feed/filters';
import { CrossIcon, SearchIcon } from './icons';

/** How long the reader stops typing before the feed is asked. */
export const SEARCH_DEBOUNCE_MS = 300;

interface SearchBoxProps {
  /** The committed query — what the URL and the loaded feed agree on. */
  value: string;
  onChange: (next: string) => void;
}

/**
 * The search box, and the two things about it that are not the input.
 *
 * **It is debounced, and the debounce is why the committed value and the
 * typed value are different pieces of state.** Each committed change is a
 * new `filterKey`, which discards every loaded page and refetches from the
 * top; doing that per keystroke would be eight requests and eight discarded
 * feeds to type "postgres". So the input holds what the reader is typing
 * and `onChange` fires once they stop.
 *
 * **It resynchronises from `value`, and only when `value` moves somewhere
 * the box did not send it.** "Clear filters" sets the query to `''` from
 * outside, as does the back button; without the effect below the input
 * would keep showing a search the feed is no longer running. Comparing
 * against the last value this box *committed* is what stops the effect
 * fighting the reader — echoing our own commit back into the input would
 * reset the cursor position mid-word for anyone who kept typing through
 * the debounce.
 */
export function SearchBox({ value, onChange }: SearchBoxProps) {
  const inputId = useId();
  const [typed, setTyped] = useState(value);
  const committed = useRef(value);

  useEffect(() => {
    if (value === committed.current) return;
    committed.current = value;
    setTyped(value);
  }, [value]);

  useEffect(() => {
    if (typed === committed.current) return undefined;
    const timer = setTimeout(() => {
      committed.current = typed;
      onChange(typed);
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [typed, onChange]);

  const commitNow = (next: string) => {
    committed.current = next;
    setTyped(next);
    onChange(next);
  };

  return (
    <div className="search">
      <label className="visually-hidden" htmlFor={inputId}>
        Search titles and summaries
      </label>
      <SearchIcon className="search__icon" />
      <input
        id={inputId}
        className="input search__input"
        // `search` rather than `text`: it is what the control is, and it is
        // what tells a browser to offer this field's history back.
        type="search"
        // The browser's own clear affordance is suppressed in the stylesheet
        // — see `search__input` — because it appears in one engine, sits
        // where our button sits, and does not reach the keyboard.
        placeholder="Search the feed"
        // Matches the server's bound, so the reader meets a box that stops
        // rather than a 422 over the whole feed.
        maxLength={MAX_QUERY_LENGTH}
        value={typed}
        onChange={(event) => {
          setTyped(event.target.value);
        }}
        onKeyDown={(event) => {
          // Enter skips the wait; Escape clears without reaching for the
          // mouse. Both commit immediately, because both are the reader
          // saying they have finished.
          if (event.key === 'Enter') commitNow(typed.trim());
          if (event.key === 'Escape' && typed !== '') {
            event.stopPropagation();
            commitNow('');
          }
        }}
      />
      {typed === '' ? null : (
        <button
          type="button"
          className="search__clear"
          onClick={() => {
            commitNow('');
          }}
        >
          <CrossIcon />
          <span className="visually-hidden">Clear the search</span>
        </button>
      )}
    </div>
  );
}
