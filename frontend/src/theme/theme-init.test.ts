import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

import { describe, expect, it } from 'vitest';

import { THEME_STORAGE_KEY } from './theme';

/**
 * `public/theme-init.js` is the anti-flash script: a blocking, same-origin,
 * non-inline `<script>` in `<head>` that stamps `data-theme` before the
 * browser paints anything. It cannot import from `src/`, because it must run
 * before the module graph loads and the production CSP forbids inlining it.
 * So it duplicates the resolution rules in `theme.ts` by hand, and nothing at
 * build time checks the two agree.
 *
 * These tests execute the real file — not a copy of its logic — in a `vm`
 * context with the three globals it touches, and assert the outcome for every
 * (stored value, OS preference) combination. A drift between the two
 * implementations shows up here rather than as a flash of the wrong theme on
 * someone's screen.
 */

const SOURCE = readFileSync(
  fileURLToPath(new URL('../../public/theme-init.js', import.meta.url)),
  'utf8',
);

interface RunResult {
  dataTheme: string | null;
  colorScheme: string | undefined;
  queriedFor: string[];
}

function run(options: { stored?: string | null; osDark?: boolean; storageThrows?: boolean }): RunResult {
  const attributes = new Map<string, string>();
  const style: Record<string, string> = {};
  const queriedFor: string[] = [];

  const context = vm.createContext({
    window: {
      localStorage: {
        getItem(key: string) {
          if (options.storageThrows) throw new Error('SecurityError');
          return key === THEME_STORAGE_KEY ? (options.stored ?? null) : null;
        },
      },
      matchMedia(query: string) {
        queriedFor.push(query);
        return { matches: query.includes('dark') ? Boolean(options.osDark) : false };
      },
    },
    document: {
      documentElement: {
        setAttribute: (name: string, value: string) => attributes.set(name, value),
        style,
      },
    },
  });

  vm.runInContext(SOURCE, context);
  return { dataTheme: attributes.get('data-theme') ?? null, colorScheme: style.colorScheme, queriedFor };
}

describe('theme-init.js', () => {
  it('uses the same storage key as theme.ts', () => {
    // The two are wired together only by this string. If they diverge, the
    // pre-paint script reads nothing and every load flashes the default.
    expect(SOURCE).toContain(`'${THEME_STORAGE_KEY}'`);
  });

  it.each([
    // stored,     OS dark, expected  — the description is the case that matters
    ['dark', false, 'dark', 'stored dark on a light OS still paints dark'],
    ['light', true, 'light', 'stored light on a dark OS still paints light'],
    ['system', true, 'dark', 'system follows a dark OS'],
    ['system', false, 'light', 'system follows a light OS'],
  ])('%s + osDark=%s resolves to %s (%s)', (stored, osDark, expected) => {
    const result = run({ stored, osDark });
    expect(result.dataTheme).toBe(expected);
    expect(result.colorScheme).toBe(expected);
  });

  it.each([
    { stored: null, why: 'nothing stored yet — a first visit' },
    { stored: '', why: 'an empty string' },
    { stored: 'solarized', why: 'a value we never write' },
    { stored: 'SYSTEM', why: 'the right word in the wrong case' },
  ])('falls back to the OS for $why', ({ stored }) => {
    expect(run({ stored, osDark: true }).dataTheme).toBe('dark');
    expect(run({ stored, osDark: false }).dataTheme).toBe('light');
  });

  it('falls back to the OS preference when localStorage throws', () => {
    // Private browsing: reading storage raises rather than returning null.
    expect(run({ storageThrows: true, osDark: true }).dataTheme).toBe('dark');
    expect(run({ storageThrows: true, osDark: false }).dataTheme).toBe('light');
  });

  it('always resolves to a concrete theme, never leaves the attribute unset', () => {
    // Whatever happens, first paint must have an explicit theme; an unset
    // attribute would fall through to the media query and could disagree
    // with the stored choice for one frame.
    for (const stored of [null, 'system', 'light', 'dark', 'nonsense']) {
      for (const osDark of [true, false]) {
        const { dataTheme, colorScheme } = run({ stored, osDark });
        expect(dataTheme).toMatch(/^(light|dark)$/);
        expect(colorScheme).toBe(dataTheme);
      }
    }
  });

  it('asks only about prefers-color-scheme: dark', () => {
    const { queriedFor } = run({ stored: 'system', osDark: true });
    expect(queriedFor).toEqual(['(prefers-color-scheme: dark)']);
  });

  it('does not consult the OS when the choice is explicit', () => {
    // Cheap, but it is also the property that makes "always dark" mean it.
    expect(run({ stored: 'dark', osDark: false }).queriedFor).toEqual([]);
    expect(run({ stored: 'light', osDark: true }).queriedFor).toEqual([]);
  });
});
