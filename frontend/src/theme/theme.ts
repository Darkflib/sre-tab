import type { Theme } from '../api/types';
import { readStoredValue, writeStoredValue } from '../lib/storage';

/**
 * Mirrored in `public/theme-init.js`, which runs before first paint. The
 * server profile is authoritative; this key only caches the last known
 * choice so the first paint is not the wrong colour.
 */
export const THEME_STORAGE_KEY = 'dnd.theme';

export type ResolvedTheme = 'light' | 'dark';

const DARK_QUERY = '(prefers-color-scheme: dark)';

export function prefersDark(): boolean {
  return typeof window !== 'undefined' && window.matchMedia(DARK_QUERY).matches;
}

export function resolveTheme(choice: Theme): ResolvedTheme {
  if (choice === 'system') return prefersDark() ? 'dark' : 'light';
  return choice;
}

export function applyTheme(resolved: ResolvedTheme): void {
  const root = document.documentElement;
  root.setAttribute('data-theme', resolved);
  // Drives the UA's own form controls, scrollbars, and canvas colour.
  root.style.colorScheme = resolved;
}

export function rememberChoice(choice: Theme): void {
  writeStoredValue(THEME_STORAGE_KEY, choice);
}

export function readRememberedChoice(): Theme {
  const stored = readStoredValue(THEME_STORAGE_KEY);
  if (stored === 'light' || stored === 'dark' || stored === 'system') return stored;
  return 'system';
}

export function subscribeToSystemTheme(onChange: () => void): () => void {
  const query = window.matchMedia(DARK_QUERY);
  query.addEventListener('change', onChange);
  return () => {
    query.removeEventListener('change', onChange);
  };
}
