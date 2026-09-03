// @vitest-environment happy-dom
import { act, createElement, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Preferences, Source, Topic, User } from '../api/types';
import { CatalogueContext, type CatalogueValue } from '../catalogue/CatalogueProvider';
import { FILTERS_COLLAPSED_STORAGE_KEY } from '../feed/collapse';
import type { FeedFilters } from '../feed/filters';
import { SessionContext, type SessionValue } from '../session/SessionProvider';
import { FilterBar } from './FilterBar';

/**
 * The disclosure, mounted for real. `../data/usePagedResource.effects.test.ts`
 * is the precedent for both halves of this: `happy-dom` opted into per file
 * through the docblock above, so the rest of the suite keeps failing loudly
 * when it reaches for a global it did not install, and a hand-rolled renderer
 * over `createRoot` and React 19's own `act` rather than a testing library —
 * a renderer costs thirty lines here and a dependency is a supply-chain
 * decision.
 *
 * What is checked here and nowhere else is the pair a static read cannot
 * settle: that `aria-expanded` tracks the region's real state rather than
 * being set once and left, and that a collapsed bar with a live selection
 * still says what it is filtering. `../feed/collapse.test.ts` covers the
 * storage rules and the wording; this covers the wiring.
 */

// --- fixtures -----------------------------------------------------------

function source(slug: string, name: string, refreshMinutes = 60): Source {
  return {
    slug,
    name,
    feed_url: `https://example.invalid/${slug}.xml`,
    website_url: `https://example.invalid/${slug}`,
    icon_url: null,
    refresh_minutes: refreshMinutes,
    enabled: true,
    topics: [],
  };
}

function topic(slug: string, name: string): Topic {
  return { slug, name, enabled: true };
}

const SOURCES = [source('lwn', 'LWN'), source('hn', 'Hacker News', 15), source('phoronix', 'Phoronix')];
const TOPICS = [topic('kernel', 'Kernel'), topic('security', 'Security')];

const CATALOGUE: CatalogueValue = {
  status: 'ready',
  sources: SOURCES,
  topics: TOPICS,
  sourceBySlug: new Map(SOURCES.map((entry) => [entry.slug, entry])),
  topicBySlug: new Map(TOPICS.map((entry) => [entry.slug, entry])),
  error: null,
  reload: () => undefined,
};

const USER: User = {
  id: 1,
  github_id: 1234567,
  github_login: 'darkflib',
  display_name: 'Mike',
  avatar_url: null,
  is_admin: false,
  created_at: '2026-01-01T00:00:00Z',
};

const PREFERENCES: Preferences = {
  theme: 'system',
  layout: 'grid',
  max_visible_cards: 50,
  onboarding_completed: true,
  topics: [],
  sources: [],
};

const SESSION: SessionValue = {
  status: 'authenticated',
  user: USER,
  preferences: PREFERENCES,
  error: null,
  reload: () => undefined,
  updatePreferences: () => Promise.resolve(),
  signOut: () => Promise.resolve(),
  removeAccount: () => Promise.resolve(),
};

const NO_FILTERS: FeedFilters = { topics: null, sources: null, readState: 'all' };

// --- the renderer -------------------------------------------------------

async function settle(work: () => void = () => undefined): Promise<void> {
  await act(async () => {
    work();
    await Promise.resolve();
  });
}

interface Harness {
  container: HTMLElement;
  unmount: () => Promise<void>;
}

function wrap(children: ReactNode): ReactNode {
  return createElement(
    SessionContext,
    { value: SESSION },
    createElement(CatalogueContext, { value: CATALOGUE }, children),
  );
}

async function mount(filters: FeedFilters = NO_FILTERS): Promise<Harness> {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);

  await settle(() => {
    root.render(
      wrap(
        createElement(FilterBar, {
          filters,
          onChange: () => undefined,
          shares: [],
          loadedCount: 0,
        }),
      ),
    );
  });

  return {
    container,
    unmount: async () => {
      await settle(() => {
        root.unmount();
      });
      container.remove();
    },
  };
}

// --- reading the rendered disclosure ------------------------------------

/**
 * Found by `aria-expanded` rather than by class, so the query itself asserts
 * the thing that matters: there is exactly one control here declaring itself
 * a disclosure.
 */
function disclosure(container: HTMLElement): HTMLButtonElement {
  const found = container.querySelectorAll('[aria-expanded]');
  if (found.length !== 1) throw new Error(`expected one disclosure, found ${found.length}`);
  const node = found[0];
  if (!(node instanceof HTMLButtonElement)) throw new Error('the disclosure is not a button');
  return node;
}

/** The region `aria-controls` names, which must exist for the attribute to
 *  mean anything at all. */
function controlled(container: HTMLElement): HTMLElement {
  const id = disclosure(container).getAttribute('aria-controls');
  if (id === null) throw new Error('the disclosure controls nothing');
  const node = document.getElementById(id);
  if (node === null) throw new Error(`aria-controls names ${id}, which is not in the document`);
  return node;
}

/**
 * Whether anything between `node` and the root has been hidden — the
 * difference between "in the DOM" and "on screen", and the one a
 * `querySelector` will happily not tell you. Every assertion about what a
 * collapsed bar still says goes through this or `visibleText`, because the
 * cheapest way to break this feature is to leave the summary inside the
 * region that gets collapsed, where it reads perfectly in a snapshot and
 * shows nobody anything.
 */
function isHidden(node: Element, root: Element): boolean {
  for (let cursor: Element | null = node; cursor !== null; cursor = cursor.parentElement) {
    if (cursor.hasAttribute('hidden')) return true;
    if (cursor === root) break;
  }
  return false;
}

/**
 * Text a sighted reader can actually see. `[hidden]` and `.visually-hidden`
 * are both cut, because both are text the DOM carries and the screen does
 * not — and this file's whole subject is the gap between those two.
 */
function visibleText(root: Element): string {
  if (root.hasAttribute('hidden') || root.classList.contains('visually-hidden')) return '';
  return [...root.childNodes]
    .map((node) => {
      if (node instanceof Element) return visibleText(node);
      return node.textContent;
    })
    .join('');
}

function summaryText(container: HTMLElement): string | null {
  const node = container.querySelector('.filters__summary');
  if (node === null) return null;
  if (isHidden(node, container)) throw new Error('the summary is inside a region that is hidden');
  return node.textContent;
}

function chipLabels(container: HTMLElement): string[] {
  return [...container.querySelectorAll('.chip__label')].map((node) => node.textContent);
}

// --- storage ------------------------------------------------------------

/** `localStorage` that raises on the property access itself, which is what
 *  Safari in private mode and a blocked-cookies setting actually do. */
function breakStorage(): () => void {
  const original = Object.getOwnPropertyDescriptor(window, 'localStorage');
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    get() {
      throw new Error('SecurityError');
    },
  });
  return () => {
    if (original === undefined) Reflect.deleteProperty(window, 'localStorage');
    else Object.defineProperty(window, 'localStorage', original);
  };
}

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  window.localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
  document.body.replaceChildren();
});

// --- the default --------------------------------------------------------

describe('the default state', () => {
  it('is expanded, with the chips on screen', async () => {
    const harness = await mount();

    expect(disclosure(harness.container).getAttribute('aria-expanded')).toBe('true');
    expect(controlled(harness.container).hidden).toBe(false);
    expect(chipLabels(harness.container)).toContain('Hacker News');

    await harness.unmount();
  });

  it('is a real button, so the keyboard reaches it without a new binding', async () => {
    const harness = await mount();
    const button = disclosure(harness.container);

    // No `role` bolted onto a div, nothing taken out of the tab order, and
    // `type="button"` so it can never submit a form it is dropped into.
    expect(button.tagName).toBe('BUTTON');
    expect(button.type).toBe('button');
    expect(button.getAttribute('tabindex')).toBeNull();
    expect(button.disabled).toBe(false);

    await harness.unmount();
  });

  it('carries a visible label and not a bare chevron', async () => {
    const harness = await mount();
    const button = disclosure(harness.container);

    // The icon is `aria-hidden`, so a label that is only in the
    // accessibility tree leaves a sighted reader guessing at an arrow.
    expect(visibleText(button).trim()).toBe('Filters');
    expect(button.textContent).toContain('Filters');

    await harness.unmount();
  });

  it('controls a region that is in the document', async () => {
    const harness = await mount();

    // An `aria-controls` naming an id nothing carries is a wrong attribute
    // that no linter here would see.
    expect(controlled(harness.container).textContent).toContain('Sources');

    await harness.unmount();
  });

  it('comes before the region it controls, so the tab order still reads', async () => {
    const harness = await mount();
    const button = disclosure(harness.container);
    const region = controlled(harness.container);

    // Tab order is DOM order here — nothing sets `tabindex` — so this is the
    // whole of "you reach the control before the thing it opens".
    expect(button.compareDocumentPosition(region) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(region.contains(button)).toBe(false);

    await harness.unmount();
  });
});

// --- toggling -----------------------------------------------------------

describe('toggling', () => {
  it('collapses, and aria-expanded follows the region rather than being set once', async () => {
    const harness = await mount();
    const button = disclosure(harness.container);

    await settle(() => {
      button.click();
    });

    expect(button.getAttribute('aria-expanded')).toBe('false');
    expect(controlled(harness.container).hidden).toBe(true);

    await harness.unmount();
  });

  it('expands again', async () => {
    const harness = await mount();
    const button = disclosure(harness.container);

    await settle(() => {
      button.click();
    });
    await settle(() => {
      button.click();
    });

    expect(button.getAttribute('aria-expanded')).toBe('true');
    expect(controlled(harness.container).hidden).toBe(false);

    await harness.unmount();
  });

  it('takes the chips out of the tab order when it hides them', async () => {
    // `hidden` rather than a class that only paints, so a keyboard user does
    // not tab through sixteen chips nobody can see.
    const harness = await mount();

    await settle(() => {
      disclosure(harness.container).click();
    });

    const region = controlled(harness.container);
    const chips = region.querySelectorAll('button');
    expect(chips.length).toBeGreaterThan(0);
    // happy-dom does not compute style, so the assertion is on the attribute
    // the platform acts on rather than on the paint.
    expect(region.hasAttribute('hidden')).toBe(true);

    await harness.unmount();
  });
});

// --- persistence --------------------------------------------------------

describe('the choice across a remount', () => {
  it('is still collapsed on the next visit', async () => {
    const first = await mount();
    await settle(() => {
      disclosure(first.container).click();
    });
    await first.unmount();

    const second = await mount();
    expect(disclosure(second.container).getAttribute('aria-expanded')).toBe('false');
    expect(controlled(second.container).hidden).toBe(true);

    await second.unmount();
  });

  it('is still expanded on the next visit after being expanded again', async () => {
    // The direction a write-only-when-collapsed implementation gets wrong,
    // and it is the worse failure of the two: a bar that will not stay open.
    const first = await mount();
    await settle(() => {
      disclosure(first.container).click();
    });
    await settle(() => {
      disclosure(first.container).click();
    });
    await first.unmount();

    const second = await mount();
    expect(disclosure(second.container).getAttribute('aria-expanded')).toBe('true');

    await second.unmount();
  });

  it('leaves a value behind that only this feature reads', async () => {
    const harness = await mount();
    await settle(() => {
      disclosure(harness.container).click();
    });

    expect(window.localStorage.getItem(FILTERS_COLLAPSED_STORAGE_KEY)).toBe('collapsed');
    // Nothing about a view preference belongs in the profile.
    expect(window.localStorage.getItem('dnd.theme')).toBeNull();

    await harness.unmount();
  });
});

describe('storage that cannot be used', () => {
  it('renders expanded rather than crashing the feed', async () => {
    const restore = breakStorage();
    try {
      const harness = await mount();
      expect(disclosure(harness.container).getAttribute('aria-expanded')).toBe('true');
      await harness.unmount();
    } finally {
      restore();
    }
  });

  it('still collapses and expands for this visit', async () => {
    const restore = breakStorage();
    try {
      const harness = await mount();
      const button = disclosure(harness.container);

      await settle(() => {
        button.click();
      });
      expect(button.getAttribute('aria-expanded')).toBe('false');
      expect(controlled(harness.container).hidden).toBe(true);

      await settle(() => {
        button.click();
      });
      expect(button.getAttribute('aria-expanded')).toBe('true');

      await harness.unmount();
    } finally {
      restore();
    }
  });
});

// --- collapsed is not inactive ------------------------------------------

describe('a collapsed bar that is still filtering', () => {
  it('names the sources it is narrowed to', async () => {
    const harness = await mount({ ...NO_FILTERS, sources: ['hn', 'lwn'] });
    await settle(() => {
      disclosure(harness.container).click();
    });

    // The chips are gone, so the names have to come from somewhere else.
    expect(chipLabels(harness.container).length).toBeGreaterThan(0);
    expect(summaryText(harness.container)).toBe('Sources: Hacker News and LWN.');

    await harness.unmount();
  });

  it('keeps the Filtered badge and the way back out on screen', async () => {
    const harness = await mount({ ...NO_FILTERS, sources: ['hn'] });
    await settle(() => {
      disclosure(harness.container).click();
    });

    // Against the visible text, not the markup: a head moved inside the
    // collapsible region would still answer a `querySelector`.
    const text = visibleText(harness.container);
    expect(text).toContain('Filtered');
    expect(text).toContain('Clear filters');
    expect(text).toContain('1 of 3 sources');

    await harness.unmount();
  });

  it('separates the dimensions it is narrowed by', async () => {
    const harness = await mount({ ...NO_FILTERS, sources: ['hn'], readState: 'unread' });
    await settle(() => {
      disclosure(harness.container).click();
    });

    expect(summaryText(harness.container)).toBe('Sources: Hacker News. Unread only.');

    await harness.unmount();
  });

  it('says outright when nothing is selected', async () => {
    // The worst version of the failure: an empty feed, no chips, and a bar
    // that looks idle.
    const harness = await mount({ ...NO_FILTERS, sources: [] });
    await settle(() => {
      disclosure(harness.container).click();
    });

    expect(summaryText(harness.container)).toBe('No sources selected.');

    await harness.unmount();
  });

  it('names a read-state narrowing, which no count could show', async () => {
    const harness = await mount({ ...NO_FILTERS, readState: 'unread' });
    await settle(() => {
      disclosure(harness.container).click();
    });

    expect(summaryText(harness.container)).toBe('Unread only.');

    await harness.unmount();
  });

  it('says nothing extra when nothing is overridden', async () => {
    // `null` is not a narrowing. A phrase here would report a filter that
    // does not exist, which is the same lie in the other direction.
    const harness = await mount();
    await settle(() => {
      disclosure(harness.container).click();
    });

    expect(summaryText(harness.container)).toBeNull();
    expect(harness.container.querySelector('.filters__state')?.textContent).toContain(
      'Your selection',
    );

    await harness.unmount();
  });

  it('says nothing while expanded, because the chips are saying it', async () => {
    const harness = await mount({ ...NO_FILTERS, sources: ['hn'] });

    expect(summaryText(harness.container)).toBeNull();

    await harness.unmount();
  });
});
