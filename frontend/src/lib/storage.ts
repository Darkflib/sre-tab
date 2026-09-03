/**
 * Every `localStorage` access in this client goes through here.
 *
 * The failure to survive is not a missing key, it is a throw. Safari in
 * private mode and a blocked-cookies setting each raise on the *property
 * access* — `window.localStorage` itself, before any key is named — so the
 * whole expression sits inside the `try`, `window` included. A context
 * without a `window` at all therefore reads as "nothing stored" rather than
 * as a crash, which is the same answer an empty storage gives and the same
 * answer every caller already has a default for.
 *
 * Nothing kept here is authoritative, and that is the condition for
 * swallowing the failure. The theme mirrors a server profile so the first
 * paint is not the wrong colour; the filter bar's collapsed state is a
 * per-device view preference the server has no column for. Losing either
 * costs a default.
 */
export function readStoredValue(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function writeStoredValue(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Private browsing or a storage quota. The value is a convenience.
  }
}
