// @vitest-environment happy-dom
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { BuildInfo } from '../lib/build';
import { BuildStamp } from './BuildStamp';

/**
 * The stamp mounted for real, on the renderer precedent in
 * `FilterBar.collapse.test.tsx`. `../lib/build.test.ts` decides which values
 * are trusted; this checks that the component only links what it was given a
 * link for, and renders everything as text.
 */

const SHA = 'b734388df3ef86f75cffe4b3f214169c02e87fbf';

async function render(info: BuildInfo): Promise<HTMLElement> {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(createElement(BuildStamp, { info }));
    await Promise.resolve();
  });
  return container;
}

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
});

afterEach(() => {
  document.body.replaceChildren();
  vi.unstubAllGlobals();
});

describe('BuildStamp', () => {
  it('links the short commit to its page', async () => {
    const container = await render({
      version: '1.1.0',
      commit: SHA,
      shortCommit: 'b734388',
      commitUrl: `https://github.com/Darkflib/sre-tab/commit/${SHA}`,
    });

    expect(container.textContent).toBe('sre-tab 1.1.0 · b734388');
    const link = container.querySelector('a');
    expect(link?.getAttribute('href')).toBe(`https://github.com/Darkflib/sre-tab/commit/${SHA}`);
    expect(link?.getAttribute('title')).toBe(SHA);
    expect(link?.getAttribute('rel')).toBe('noopener noreferrer');
  });

  it('shows the commit without a link when there is nowhere to send it', async () => {
    const container = await render({
      version: '1.1.0',
      commit: SHA,
      shortCommit: 'b734388',
      commitUrl: null,
    });

    expect(container.textContent).toBe('sre-tab 1.1.0 · b734388');
    expect(container.querySelector('a')).toBeNull();
  });

  it('says it is a development build when it has no commit', async () => {
    const container = await render({
      version: '1.1.0',
      commit: null,
      shortCommit: null,
      commitUrl: null,
    });

    expect(container.textContent).toBe('sre-tab 1.1.0 · development build');
    expect(container.querySelector('a')).toBeNull();
  });
});
