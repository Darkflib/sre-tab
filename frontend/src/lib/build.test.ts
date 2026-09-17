import { describe, expect, it } from 'vitest';

import { BUILD, buildInfo } from './build';

const SHA = 'b734388df3ef86f75cffe4b3f214169c02e87fbf';
const REPO = 'https://github.com/Darkflib/sre-tab';

describe('buildInfo', () => {
  it('reports the commit and links it when the build was told both', () => {
    expect(buildInfo({ VITE_SRE_TAB_COMMIT: SHA, VITE_SRE_TAB_SOURCE_URL: REPO }, '1.1.0')).toEqual({
      version: '1.1.0',
      commit: SHA,
      shortCommit: 'b734388',
      commitUrl: `${REPO}/commit/${SHA}`,
    });
  });

  it('says nothing about a commit when the build was not given one', () => {
    for (const commit of [undefined, '', '   ']) {
      const info = buildInfo({ VITE_SRE_TAB_COMMIT: commit, VITE_SRE_TAB_SOURCE_URL: REPO }, '1.1.0');
      expect(info).toMatchObject({ commit: null, shortCommit: null, commitUrl: null });
    }
  });

  it.each([
    ['a short commit', 'b734388'],
    ['a branch name', 'main'],
    ['a sha with a path appended', `${SHA}/../../settings`],
    ['a sha-256 digest', 'a'.repeat(64)],
  ])('refuses %s rather than showing it as a commit', (_label, commit) => {
    const info = buildInfo({ VITE_SRE_TAB_COMMIT: commit, VITE_SRE_TAB_SOURCE_URL: REPO }, '1.1.0');
    expect(info.commit).toBeNull();
    expect(info.commitUrl).toBeNull();
  });

  it('normalises case and surrounding whitespace in the commit', () => {
    expect(buildInfo({ VITE_SRE_TAB_COMMIT: ` ${SHA.toUpperCase()}\n` }, '1.1.0').commit).toBe(SHA);
  });

  it('keeps the commit but drops the link when the source is missing', () => {
    expect(buildInfo({ VITE_SRE_TAB_COMMIT: SHA }, '1.1.0')).toMatchObject({
      shortCommit: 'b734388',
      commitUrl: null,
    });
  });

  it.each([
    ['plain http', 'http://github.com/Darkflib/sre-tab'],
    ['a script URL', 'javascript:alert(1)'],
    ['credentials', 'https://user:pass@github.com/Darkflib/sre-tab'],
    ['a query', 'https://github.com/Darkflib/sre-tab?next=/evil'],
    ['a fragment', 'https://github.com/Darkflib/sre-tab#x'],
    ['not a URL', 'github.com/Darkflib/sre-tab'],
  ])('links nothing for a source with %s', (_label, source) => {
    const info = buildInfo({ VITE_SRE_TAB_COMMIT: SHA, VITE_SRE_TAB_SOURCE_URL: source }, '1.1.0');
    expect(info.shortCommit).toBe('b734388');
    expect(info.commitUrl).toBeNull();
  });

  it('does not double the slash when the source ends in one', () => {
    expect(
      buildInfo({ VITE_SRE_TAB_COMMIT: SHA, VITE_SRE_TAB_SOURCE_URL: `${REPO}/` }, '1.1.0').commitUrl,
    ).toBe(`${REPO}/commit/${SHA}`);
  });
});

describe('BUILD', () => {
  it('carries package.json’s version through the define in vite.config.ts', async () => {
    const manifest = (await import('../../package.json')).default as { version: string };
    expect(BUILD.version).toBe(manifest.version);
  });
});
