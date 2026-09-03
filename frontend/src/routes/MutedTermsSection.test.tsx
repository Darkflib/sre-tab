// @vitest-environment happy-dom
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Preferences, PreferencesPatch, Topic } from '../api/types';
import { MAX_TERMS, MutedTermsSection } from './MutedTermsSection';

/**
 * The muted list, mounted for real. Renderer per
 * `../components/FilterBar.collapse.test.tsx`.
 *
 * What is worth checking here is not that a form submits. It is the set of
 * ways this screen could quietly send the server a mute the reader did not
 * mean, or refuse one they did — because a mute is the one setting whose
 * effect is invisible on the feed, so a wrong entry here is a feed that is
 * missing things for no reason the reader can see.
 */

const TOPICS: Topic[] = [
  { slug: 'uk-news', name: 'UK news', enabled: true },
  { slug: 'webdev', name: 'Web development', enabled: true },
  { slug: 'legacy', name: 'Legacy', enabled: false },
];

function preferences(overrides: Partial<Preferences> = {}): Preferences {
  return {
    theme: 'system',
    layout: 'grid',
    max_visible_cards: 50,
    onboarding_completed: true,
    topics: [],
    sources: [],
    muted_words: [],
    muted_tags: [],
    ...overrides,
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

function render(prefs: Preferences, onSave: (patch: PreferencesPatch) => void): void {
  act(() => {
    root.render(
      createElement(MutedTermsSection, { preferences: prefs, topics: TOPICS, onSave }),
    );
  });
}

function wordInput(): HTMLInputElement {
  const field = host.querySelector<HTMLInputElement>('#mute-word');
  if (!field) throw new Error('no word input rendered');
  return field;
}

function type(text: string): void {
  const descriptor = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
  if (!descriptor?.set) throw new Error('HTMLInputElement has no value setter');
  act(() => {
    const field = wordInput();
    descriptor.set?.call(field, text);
    field.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

function submit(): void {
  act(() => {
    host.querySelector('form')?.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  });
}

function addButton(): HTMLButtonElement {
  const button = host.querySelector<HTMLButtonElement>('button[type="submit"]');
  if (!button) throw new Error('no submit button rendered');
  return button;
}

describe('MutedTermsSection', () => {
  it('adds a word to the existing list rather than replacing it', () => {
    // The patch is replace-the-whole-list, so sending only the new term
    // would silently unmute everything already there.
    const onSave = vi.fn();
    render(preferences({ muted_words: ['derby'] }), onSave);

    type('football');
    submit();

    expect(onSave.mock.calls).toEqual([[{ muted_words: ['derby', 'football'] }]]);
  });

  it('normalises before sending, so the reader sees what the server stored', () => {
    // The server normalises anyway. Doing it here too is what makes the
    // duplicate check below honest — otherwise "  Football " looks new.
    const onSave = vi.fn();
    render(preferences(), onSave);

    type('  Premier   League  ');
    submit();

    expect(onSave.mock.calls).toEqual([[{ muted_words: ['premier league'] }]]);
  });

  it('refuses a duplicate rather than sending a save that changes nothing', () => {
    const onSave = vi.fn();
    render(preferences({ muted_words: ['football'] }), onSave);

    type('FOOTBALL');

    expect(addButton().disabled).toBe(true);
    submit();
    expect(onSave).not.toHaveBeenCalled();
    expect(host.textContent).toContain('already muted');
  });

  it('refuses an empty or whitespace-only term', () => {
    // The term that would match every item. The server drops it too; this
    // is so the reader never sends one and wonders where the feed went.
    const onSave = vi.fn();
    render(preferences(), onSave);

    type('   ');
    expect(addButton().disabled).toBe(true);
    submit();

    expect(onSave).not.toHaveBeenCalled();
  });

  it('stops at the number of terms the server accepts', () => {
    const onSave = vi.fn();
    render(preferences({ muted_words: Array.from({ length: MAX_TERMS }, (_, n) => `w${String(n)}`) }), onSave);

    type('football');

    expect(addButton().disabled).toBe(true);
    expect(host.textContent).toContain('which is the limit');
  });

  it('bounds a term at the length the column holds', () => {
    render(preferences(), vi.fn());

    expect(wordInput().maxLength).toBe(64);
  });

  it('removes one term without disturbing the others', () => {
    const onSave = vi.fn();
    render(preferences({ muted_words: ['derby', 'football'] }), onSave);

    const remove = host.querySelectorAll<HTMLButtonElement>('.muted__remove');
    act(() => {
      remove[0].click();
    });

    expect(onSave.mock.calls).toEqual([[{ muted_words: ['football'] }]]);
  });

  it('names each remove control by the term it removes', () => {
    // Eight identical "Remove" buttons is a list nobody can use from a
    // screen reader, on the one screen that exists to be audited.
    render(preferences({ muted_words: ['derby', 'football'] }), vi.fn());

    const labels = [...host.querySelectorAll('.muted__remove')].map((node) => node.textContent);

    expect(labels).toEqual(['Stop muting derby', 'Stop muting football']);
  });

  it('offers only topics the catalogue has enabled', () => {
    // The server refuses a tag naming no topic. Offering a disabled one
    // would put the reader in front of that refusal for no reason.
    render(preferences(), vi.fn());

    const labels = [...host.querySelectorAll('.option__label')].map((node) => node.textContent);

    expect(labels).toEqual(['UK news', 'Web development']);
  });

  it('toggles a muted topic without touching the muted words', () => {
    const onSave = vi.fn();
    render(preferences({ muted_words: ['derby'], muted_tags: ['uk-news'] }), onSave);

    const boxes = host.querySelectorAll<HTMLInputElement>('.option input');
    act(() => {
      boxes[1].click();
    });

    expect(onSave.mock.calls).toEqual([[{ muted_tags: ['uk-news', 'webdev'] }]]);
  });

  it('says bookmarks are never muted', () => {
    // A reader's saved items are the thing they would most fear losing to
    // a setting like this, and the server exempts them. Saying so is the
    // difference between a promise and an implementation detail.
    render(preferences(), vi.fn());

    expect(host.textContent).toContain('Bookmarks are never muted');
  });
});
