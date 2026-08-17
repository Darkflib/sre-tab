import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  applyTheme,
  prefersDark,
  readRememberedChoice,
  rememberChoice,
  resolveTheme,
  subscribeToSystemTheme,
  THEME_STORAGE_KEY,
} from './theme';

/**
 * These tests run in vitest's default `node` environment, so there is no DOM
 * and every global the module touches is installed explicitly below. That is
 * deliberate: the point is to pin down exactly which globals the theme layer
 * depends on. A jsdom environment would supply them silently and a new
 * dependency on, say, `window.sessionStorage` would slip in unnoticed.
 */

interface StorageStub {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

function installWindow(options: {
  dark?: boolean;
  storage?: StorageStub | null;
  onAdd?: (type: string, fn: () => void) => void;
  onRemove?: (type: string, fn: () => void) => void;
}) {
  const listeners = new Set<() => void>();
  const win = {
    matchMedia: (query: string) => ({
      matches: query.includes('dark') ? Boolean(options.dark) : false,
      addEventListener: (type: string, fn: () => void) => {
        listeners.add(fn);
        options.onAdd?.(type, fn);
      },
      removeEventListener: (type: string, fn: () => void) => {
        listeners.delete(fn);
        options.onRemove?.(type, fn);
      },
    }),
    localStorage: options.storage ?? undefined,
  };
  vi.stubGlobal('window', win);
  return { listeners };
}

function installDocument() {
  const attributes = new Map<string, string>();
  const style: Record<string, string> = {};
  vi.stubGlobal('document', {
    documentElement: {
      setAttribute: (name: string, value: string) => attributes.set(name, value),
      getAttribute: (name: string) => attributes.get(name) ?? null,
      style,
    },
  });
  return { attributes, style };
}

function memoryStorage(initial: Record<string, string> = {}): StorageStub {
  const map = new Map(Object.entries(initial));
  return {
    getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => {
      map.set(key, value);
    },
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('resolveTheme', () => {
  it('passes an explicit choice straight through, whatever the OS says', () => {
    installWindow({ dark: true });
    expect(resolveTheme('light')).toBe('light');
    expect(resolveTheme('dark')).toBe('dark');

    vi.unstubAllGlobals();
    installWindow({ dark: false });
    expect(resolveTheme('light')).toBe('light');
    expect(resolveTheme('dark')).toBe('dark');
  });

  it('follows the OS when the choice is system', () => {
    installWindow({ dark: true });
    expect(resolveTheme('system')).toBe('dark');

    vi.unstubAllGlobals();
    installWindow({ dark: false });
    expect(resolveTheme('system')).toBe('light');
  });
});

describe('prefersDark', () => {
  it('reports the media query result', () => {
    installWindow({ dark: true });
    expect(prefersDark()).toBe(true);

    vi.unstubAllGlobals();
    installWindow({ dark: false });
    expect(prefersDark()).toBe(false);
  });
});

describe('readRememberedChoice', () => {
  it('returns each of the three valid stored values', () => {
    for (const stored of ['light', 'dark', 'system'] as const) {
      vi.unstubAllGlobals();
      installWindow({ storage: memoryStorage({ [THEME_STORAGE_KEY]: stored }) });
      expect(readRememberedChoice()).toBe(stored);
    }
  });

  it('defaults to system when nothing is stored', () => {
    installWindow({ storage: memoryStorage() });
    expect(readRememberedChoice()).toBe('system');
  });

  it('defaults to system when the stored value is not one we wrote', () => {
    // A stale key from an older build, or another tab writing nonsense.
    installWindow({ storage: memoryStorage({ [THEME_STORAGE_KEY]: 'solarized' }) });
    expect(readRememberedChoice()).toBe('system');
  });

  it('defaults to system when storage throws', () => {
    // Safari in private mode, or a blocked-cookies setting.
    installWindow({
      storage: {
        getItem: () => {
          throw new Error('SecurityError');
        },
        setItem: () => undefined,
      },
    });
    expect(readRememberedChoice()).toBe('system');
  });
});

describe('rememberChoice', () => {
  it('writes the choice under the shared key', () => {
    const storage = memoryStorage();
    installWindow({ storage });
    rememberChoice('dark');
    expect(storage.getItem(THEME_STORAGE_KEY)).toBe('dark');
  });

  it('never throws when storage is unavailable', () => {
    installWindow({
      storage: {
        getItem: () => null,
        setItem: () => {
          throw new Error('QuotaExceededError');
        },
      },
    });
    // The paint hint is optional; failing to cache it must not break sign-in.
    expect(() => {
      rememberChoice('light');
    }).not.toThrow();
  });
});

describe('applyTheme', () => {
  it('sets both the attribute the CSS selects on and the UA colour scheme', () => {
    installWindow({});
    const { attributes, style } = installDocument();

    applyTheme('dark');
    expect(attributes.get('data-theme')).toBe('dark');
    expect(style.colorScheme).toBe('dark');

    applyTheme('light');
    expect(attributes.get('data-theme')).toBe('light');
    expect(style.colorScheme).toBe('light');
  });
});

describe('subscribeToSystemTheme', () => {
  it('subscribes to change and unsubscribes the same handler', () => {
    const added: [string, () => void][] = [];
    const removed: [string, () => void][] = [];
    installWindow({
      onAdd: (type, fn) => added.push([type, fn]),
      onRemove: (type, fn) => removed.push([type, fn]),
    });

    const handler = () => undefined;
    const unsubscribe = subscribeToSystemTheme(handler);
    expect(added).toEqual([['change', handler]]);
    expect(removed).toHaveLength(0);

    unsubscribe();
    expect(removed).toEqual([['change', handler]]);
  });
});
