/*
 * Runs before first paint, synchronously, from a same-origin file (the
 * production CSP forbids inline script). Server-side preferences are
 * authoritative but only arrive with GET /me, so the last known choice is
 * mirrored into localStorage purely as a paint hint. Keep the storage key
 * in step with THEME_STORAGE_KEY in src/theme/theme.ts.
 */
(function () {
  var KEY = 'dnd.theme';
  var stored;
  try {
    stored = window.localStorage.getItem(KEY);
  } catch {
    stored = null;
  }
  var choice = stored === 'light' || stored === 'dark' || stored === 'system' ? stored : 'system';
  var resolved =
    choice === 'system'
      ? window.matchMedia('(prefers-color-scheme: dark)').matches
        ? 'dark'
        : 'light'
      : choice;
  var root = document.documentElement;
  root.setAttribute('data-theme', resolved);
  root.style.colorScheme = resolved;
})();
