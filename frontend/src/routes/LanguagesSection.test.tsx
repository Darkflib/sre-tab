// @vitest-environment happy-dom
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Preferences, PreferencesPatch } from '../api/types';
import { describeLanguages, languageName, preferredLanguage } from '../lib/languages';
import { LanguagesSection, MAX_LANGUAGES } from './LanguagesSection';

/**
 * The language list, mounted for real. Renderer per
 * `./MutedTermsSection.test.tsx`.
 *
 * What matters is what reaches the server: the whole list every time,
 * because the field is replace-the-whole-list and a patch carrying only the
 * new code would drop every language already chosen.
 */

function preferences(languages: string[]): Preferences {
  return {
    theme: 'system',
    layout: 'grid',
    max_visible_cards: 50,
    onboarding_completed: true,
    topics: [],
    sources: [],
    muted_words: [],
    muted_tags: [],
    muted_urls: [],
    languages,
  };
}

let host: HTMLDivElement;
let root: ReturnType<typeof createRoot>;

beforeEach(() => {
  host = document.createElement('div');
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => {
    root.unmount();
  });
  host.remove();
});

function render(languages: string[], onSave: (patch: PreferencesPatch) => void): void {
  act(() => {
    root.render(createElement(LanguagesSection, { preferences: preferences(languages), onSave }));
  });
}

function picker(): HTMLSelectElement {
  const field = host.querySelector<HTMLSelectElement>('#language-add');
  if (!field) throw new Error('no language picker rendered');
  return field;
}

function choose(code: string): void {
  act(() => {
    const field = picker();
    field.value = code;
    field.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

function submit(): void {
  act(() => {
    host.querySelector('form')?.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  });
}

/** Thirty-two, the cap, spelled out so the test says what it is at. */
const AT_THE_CAP = [
  'en', 'pt', 'es', 'de', 'fr', 'it', 'nl', 'sv', 'da', 'no', 'fi', 'pl', 'cs', 'sk', 'hu', 'ro',
  'bg', 'el', 'tr', 'ru', 'uk', 'ja', 'zh', 'ko', 'th', 'vi', 'id', 'ms', 'hi', 'ar', 'he', 'fa',
];

describe('LanguagesSection', () => {
  it('says every language is shown when none is chosen', () => {
    render([], vi.fn());

    expect(host.textContent).toContain('Every language is shown.');
  });

  it('adds a language to the existing list rather than replacing it', () => {
    const onSave = vi.fn();
    render(['en'], onSave);

    choose('pt');
    submit();

    expect(onSave).toHaveBeenCalledWith({ languages: ['en', 'pt'] });
  });

  it('does not offer a language already chosen', () => {
    render(['en'], vi.fn());

    const offered = Array.from(picker().options, (option) => option.value);
    expect(offered).not.toContain('en');
    expect(offered).toContain('pt');
  });

  it('removes one language and keeps the rest', () => {
    const onSave = vi.fn();
    render(['en', 'pt'], onSave);

    const remove = Array.from(host.querySelectorAll('button')).find((button) =>
      button.textContent.includes(`Stop showing only ${languageName('pt')}`),
    );
    act(() => {
      remove?.click();
    });

    expect(onSave).toHaveBeenCalledWith({ languages: ['en'] });
  });

  it('stops adding at the cap but still lets a language come off', () => {
    const onSave = vi.fn();
    expect(AT_THE_CAP).toHaveLength(MAX_LANGUAGES);
    render(AT_THE_CAP, onSave);

    expect(picker().disabled).toBe(true);
    submit();
    expect(onSave).not.toHaveBeenCalled();

    act(() => {
      host.querySelector<HTMLButtonElement>('.muted__remove')?.click();
    });
    expect(onSave).toHaveBeenCalledWith({ languages: AT_THE_CAP.slice(1) });
  });
});

describe('language names', () => {
  it('names a code in the given locale', () => {
    expect(languageName('pt', 'en')).toBe('Portuguese');
  });

  it('calls als Alemannic, as the detector means it, not Tosk Albanian', () => {
    expect(languageName('als', 'en')).toBe('Alemannic');
  });

  it('joins names the way the mute line does', () => {
    expect(describeLanguages(['en'], 'en')).toBe('English');
    expect(describeLanguages(['en', 'pt'], 'en')).toBe('English and Portuguese');
    expect(describeLanguages(['en', 'pt', 'es'], 'en')).toBe('English, Portuguese, and Spanish');
  });

  it('offers the browser language without its region, else English', () => {
    expect(preferredLanguage(['pt-BR', 'en-GB'])).toBe('pt');
    expect(preferredLanguage(['tlh', 'en-GB'])).toBe('en');
    expect(preferredLanguage([])).toBe('en');
  });
});
