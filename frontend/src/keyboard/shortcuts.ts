/**
 * The decision half of the feed's keyboard layer: given one keystroke and
 * where the cursor is, what should happen. Nothing here touches the DOM.
 *
 * The split is the point. Focus, `<dialog>`, and the document listener live
 * in `useListKeyboard.ts` and are deliberately thin, because this project's
 * suite runs without jsdom on purpose and nothing is being added for a
 * shortcut table. Every guard that matters — typing, modifiers, the ends of
 * the list — is a value returned from a plain function, so it is checked
 * directly rather than inferred from a page nobody rendered.
 */

/** What the keystroke landed on, which decides whether it is ours at all. */
export type TargetKind = 'typing' | 'activatable' | 'passive';

/**
 * A keystroke, reduced to what a binding may depend on.
 *
 * `shiftKey` is absent on purpose. "Ignore anything with a modifier" is the
 * obvious rule and it silently takes `?` with it — on most layouts that key
 * *is* Shift+/ — so the field is not here to be checked by mistake. It is
 * also unnecessary: `event.key` already carries the shifted character, so
 * `j` and `J` are different strings and a binding on `'j'` cannot match a
 * capital. Ctrl, Cmd, and Alt are checked, because those combinations
 * belong to the browser and the OS rather than to us.
 */
export interface KeyPress {
  key: string;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
  target: TargetKind;
}

export interface CursorState {
  /** Index of the card the cursor sits on, or -1 if it has never been on one. */
  index: number;
  count: number;
  /**
   * Whether focus is *currently* inside a card. The cursor is the focus
   * ring and nothing else, so this is also the answer to "can the user see
   * what a keystroke would act on".
   */
  onCard: boolean;
  helpOpen: boolean;
}

export type KeyAction =
  | { type: 'ignore' }
  | { type: 'move'; to: number }
  | { type: 'open' }
  | { type: 'toggleRead' }
  | { type: 'toggleBookmark' }
  | { type: 'reload' }
  | { type: 'openHelp' }
  | { type: 'closeHelp' };

const IGNORE: KeyAction = { type: 'ignore' };

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}

// --- what the keystroke landed on ---------------------------------------

const TYPING_TAGS = new Set(['INPUT', 'TEXTAREA', 'SELECT']);
const TYPING_ROLES = new Set(['textbox', 'searchbox', 'combobox', 'spinbutton']);
const ACTIVATABLE_TAGS = new Set(['A', 'BUTTON', 'DETAILS', 'OPTION', 'SUMMARY']);
const ACTIVATABLE_ROLES = new Set([
  'button',
  'link',
  'menuitem',
  'menuitemcheckbox',
  'menuitemradio',
  'option',
  'switch',
  'tab',
]);

/**
 * Duck-typed rather than taking an `Element`, so the classification can be
 * exercised with plain objects. This is the guard that must never be wrong:
 * a shortcut firing while someone types is not a rough edge, it is the
 * keyboard layer eating their input.
 *
 * `<select>` is grouped with the text fields even though nothing is typed
 * into it — the arrow keys change its value, so it owns them too.
 */
export function classifyTarget(target: unknown): TargetKind {
  if (typeof target !== 'object' || target === null) return 'passive';
  const node = target as { tagName?: unknown; isContentEditable?: unknown; getAttribute?: unknown };

  // True on descendants as well, so a caret parked inside a nested element
  // of a rich-text field is caught along with the field itself.
  if (node.isContentEditable === true) return 'typing';

  const role = readRole(node);
  if (role !== null && TYPING_ROLES.has(role)) return 'typing';

  const tag = typeof node.tagName === 'string' ? node.tagName.toUpperCase() : '';
  if (TYPING_TAGS.has(tag)) return 'typing';

  if (role !== null && ACTIVATABLE_ROLES.has(role)) return 'activatable';
  if (ACTIVATABLE_TAGS.has(tag)) return 'activatable';

  return 'passive';
}

function readRole(node: { getAttribute?: unknown }): string | null {
  if (typeof node.getAttribute !== 'function') return null;
  const read = node.getAttribute as (name: string) => unknown;
  const value = read('role');
  return typeof value === 'string' ? value.trim().toLowerCase() : null;
}

// --- the bindings --------------------------------------------------------

/**
 * `j`/`k`/`o`/`m`/`b`/`?` are the conventional core — Gmail, Reeder, and
 * every Hacker News userscript agree on roughly this set, and a reader's
 * muscle memory is built somewhere else before it arrives here.
 *
 * Three judgements sit on top of that:
 *
 * - **The arrows, Home, and End only act once the cursor is on a card.**
 *   Off the list they are how the page is scrolled, and taking them from
 *   everyone who never presses `j` would be a straight regression. On a
 *   card they are what the roving-tabindex pattern is expected to answer.
 * - **`j` and `k` are the only movement keys that work off the list**,
 *   because they are what puts the cursor back on screen. Landing on the
 *   cursor rather than stepping past it means a card is never skipped
 *   unseen.
 * - **`r` reloads.** Less conventional, and it earns the key here
 *   specifically: marking an item read does not remove it from a feed
 *   narrowed to "Unread" (see `useListKeyboard`), so `r` is how the user
 *   asks for the list to catch up — at a moment they chose, rather than
 *   under their cursor mid-triage.
 *
 * Everything else that acts — `o`, `Enter`, `m`, `b` — requires the cursor
 * to be visible, because the cursor *is* the focus ring: acting on one
 * nobody can see is how a keystroke becomes a surprise.
 */
export function resolveKey(press: KeyPress, cursor: CursorState): KeyAction {
  // Typing wins over everything, before any other question is asked.
  if (press.target === 'typing') return IGNORE;
  if (press.ctrlKey || press.metaKey || press.altKey) return IGNORE;

  // While the overlay is up the list is inert behind it, so a `j` that
  // moved an invisible cursor would be worse than doing nothing.
  if (cursor.helpOpen) {
    return press.key === 'Escape' || press.key === '?' ? { type: 'closeHelp' } : IGNORE;
  }

  if (press.key === '?') return { type: 'openHelp' };
  // Before the empty-list guard: an empty feed is exactly when a refresh
  // is worth asking for.
  if (press.key === 'r') return { type: 'reload' };

  const { index, count, onCard } = cursor;
  if (count === 0) return IGNORE;

  if (press.key === 'j' || press.key === 'k') {
    if (!onCard) return { type: 'move', to: index < 0 ? 0 : clamp(index, 0, count - 1) };
    const step = press.key === 'j' ? 1 : -1;
    return { type: 'move', to: clamp(index + step, 0, count - 1) };
  }

  if (!onCard || index < 0) return IGNORE;

  switch (press.key) {
    case 'ArrowDown':
      return { type: 'move', to: clamp(index + 1, 0, count - 1) };
    case 'ArrowUp':
      return { type: 'move', to: clamp(index - 1, 0, count - 1) };
    case 'Home':
      return { type: 'move', to: 0 };
    case 'End':
      return { type: 'move', to: count - 1 };
    case 'o':
      return { type: 'open' };
    // Enter belongs to whatever is focused. On the card itself that is the
    // article, so it opens; on one of the card's own buttons it is the
    // button's, and taking it would break the Bookmark control.
    case 'Enter':
      return press.target === 'passive' ? { type: 'open' } : IGNORE;
    case 'm':
      return { type: 'toggleRead' };
    case 'b':
      return { type: 'toggleBookmark' };
    default:
      return IGNORE;
  }
}

// --- the cursor ----------------------------------------------------------

/** Where the cursor was, kept by id *and* position for when the id vanishes. */
export interface CursorAnchor {
  id: number;
  index: number;
}

/**
 * Where the cursor goes when the list changes underneath it.
 *
 * By id first, because an infinite-scroll append shifts nothing but a
 * reload renumbers everything; by remembered position second, because that
 * is what "the row that took its place" means when the item the user was
 * on has genuinely gone. `null` only when there is nothing left to sit on,
 * and the caller then has to put focus somewhere real — losing it to
 * `<body>` strands a keyboard user with no cursor and no way back to one
 * except the mouse.
 */
export function resolveCursor(previous: CursorAnchor | null, ids: number[]): number | null {
  if (previous === null || ids.length === 0) return null;
  if (ids.includes(previous.id)) return previous.id;
  return ids[clamp(previous.index, 0, ids.length - 1)];
}

/**
 * The one card in the tab order. Roving tabindex needs a stop even before
 * the user has touched the list, otherwise Tab skips the whole thing and
 * there is no way in from the keyboard at all; the first card is that stop
 * until the cursor exists.
 */
export function tabStopId(cursorId: number | null, ids: number[]): number | null {
  if (ids.length === 0) return null;
  if (cursorId !== null && ids.includes(cursorId)) return cursorId;
  return ids[0];
}

// --- discoverability -----------------------------------------------------

export interface ShortcutDoc {
  /** `KeyboardEvent.key` values, so the overlay cannot drift from the bindings. */
  keys: string[];
  description: string;
  action: Exclude<KeyAction['type'], 'ignore'>;
}

/**
 * What the `?` overlay lists. Keyed by the real `KeyboardEvent.key` values
 * rather than by prose, so a test can press each one through `resolveKey`
 * and fail if the table starts describing a binding that no longer exists.
 */
export const SHORTCUT_HELP: ShortcutDoc[] = [
  { keys: ['j', 'ArrowDown'], description: 'Next item', action: 'move' },
  { keys: ['k', 'ArrowUp'], description: 'Previous item', action: 'move' },
  { keys: ['Home', 'End'], description: 'First or last loaded item', action: 'move' },
  { keys: ['o', 'Enter'], description: 'Open in a new tab, and mark it read', action: 'open' },
  { keys: ['m'], description: 'Mark read or unread', action: 'toggleRead' },
  { keys: ['b'], description: 'Bookmark or un-bookmark', action: 'toggleBookmark' },
  { keys: ['r'], description: 'Refresh the list', action: 'reload' },
  { keys: ['?'], description: 'Show this list', action: 'openHelp' },
  { keys: ['Escape'], description: 'Close this list', action: 'closeHelp' },
];

const KEY_LABELS: Record<string, string> = {
  ArrowDown: '↓',
  ArrowUp: '↑',
  Escape: 'Esc',
};

/** How a `KeyboardEvent.key` value is drawn on a `<kbd>`. */
export function keyLabel(key: string): string {
  return Object.hasOwn(KEY_LABELS, key) ? KEY_LABELS[key] : key;
}
