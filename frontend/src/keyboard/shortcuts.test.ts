import { describe, expect, it } from 'vitest';

import {
  classifyTarget,
  type CursorState,
  type KeyAction,
  type KeyPress,
  keyLabel,
  resolveCursor,
  resolveKey,
  SHORTCUT_HELP,
  tabStopId,
  type TargetKind,
} from './shortcuts';

/**
 * The whole keyboard layer is split so that this file can exist. Deciding
 * what a keystroke means is a pure function of the key, the modifiers, what
 * the key landed on, and where the cursor is; only `.focus()` and the
 * `<dialog>` need a browser, and those stay in `useListKeyboard.ts`.
 *
 * These run in vitest's default `node` environment, like the rest of the
 * suite. Nothing is stubbed, because nothing here reaches for a global —
 * `classifyTarget` is duck-typed precisely so a plain object stands in for
 * an element, which is also the honest test: it says exactly which
 * properties of an element the guard depends on.
 *
 * Two of these cases are the ones worth being careful about.
 *
 * The typing guard is not a nicety. A feed reader that swallows a
 * keystroke while someone is filling in a field has not got a rough edge,
 * it has eaten their input, and the failure is silent from the code's side.
 *
 * `?` is the reason `shiftKey` is not in `KeyPress` at all. On most layouts
 * `?` *is* Shift+/, so the obvious rule — ignore anything carrying a
 * modifier — kills the help key and does it quietly, since `?` is also the
 * one binding a user reaches for when nothing else is working.
 */

// --- fixtures -----------------------------------------------------------

function press(key: string, overrides: Partial<KeyPress> = {}): KeyPress {
  return { key, ctrlKey: false, metaKey: false, altKey: false, target: 'passive', ...overrides };
}

function cursor(overrides: Partial<CursorState> = {}): CursorState {
  return { index: 2, count: 5, onCard: true, helpOpen: false, ...overrides };
}

const IGNORE: KeyAction = { type: 'ignore' };

// --- classifyTarget -----------------------------------------------------

describe('classifyTarget', () => {
  it.each(['INPUT', 'TEXTAREA', 'SELECT'])('treats <%s> as typing', (tagName) => {
    expect(classifyTarget({ tagName })).toBe<TargetKind>('typing');
  });

  it('matches the tag name case-insensitively', () => {
    // `tagName` is upper case on HTML elements and lower case on XML ones;
    // an SVG-hosted field should not become a live shortcut surface.
    expect(classifyTarget({ tagName: 'textarea' })).toBe<TargetKind>('typing');
  });

  it('treats a contenteditable host as typing', () => {
    expect(classifyTarget({ tagName: 'DIV', isContentEditable: true })).toBe<TargetKind>('typing');
  });

  it('treats a node inside a contenteditable as typing', () => {
    // `isContentEditable` is true on descendants too, which is the whole
    // reason it is read instead of the attribute: the caret is usually in
    // some nested <b>, not in the editable host itself.
    expect(classifyTarget({ tagName: 'B', isContentEditable: true })).toBe<TargetKind>('typing');
  });

  it.each(['textbox', 'searchbox', 'combobox', 'spinbutton'])(
    'treats role="%s" as typing whatever the tag is',
    (role) => {
      expect(classifyTarget({ tagName: 'DIV', getAttribute: () => role })).toBe<TargetKind>('typing');
    },
  );

  it.each(['A', 'BUTTON', 'SUMMARY', 'DETAILS', 'OPTION'])('treats <%s> as activatable', (tagName) => {
    expect(classifyTarget({ tagName })).toBe<TargetKind>('activatable');
  });

  it.each(['button', 'link', 'menuitem', 'tab', 'switch'])('treats role="%s" as activatable', (role) => {
    expect(classifyTarget({ tagName: 'SPAN', getAttribute: () => role })).toBe<TargetKind>('activatable');
  });

  it('treats an ordinary element as passive', () => {
    expect(classifyTarget({ tagName: 'ARTICLE' })).toBe<TargetKind>('passive');
  });

  it('reads the role attribute by name rather than taking whatever it is handed', () => {
    const node = { tagName: 'DIV', getAttribute: (name: string) => (name === 'role' ? 'button' : 'textbox') };
    expect(classifyTarget(node)).toBe<TargetKind>('activatable');
  });

  it('tolerates a role with stray case and whitespace', () => {
    expect(classifyTarget({ tagName: 'DIV', getAttribute: () => ' TextBox ' })).toBe<TargetKind>('typing');
  });

  it.each([
    ['null', null],
    ['undefined', undefined],
    ['a string', 'INPUT'],
    ['a number', 7],
  ])('falls back to passive for %s rather than throwing', (_label, target) => {
    // `event.target` is typed `EventTarget | null`, so this is reachable
    // rather than defensive: a keystroke with nothing focused still has to
    // resolve to something.
    expect(classifyTarget(target)).toBe<TargetKind>('passive');
  });

  it('falls back to passive when the node has no usable tag or role', () => {
    expect(classifyTarget({})).toBe<TargetKind>('passive');
  });
});

// --- the typing guard ---------------------------------------------------

describe('resolveKey: never hijacks typing', () => {
  it.each(['j', 'k', 'o', 'm', 'b', 'r', '?', 'Enter', 'ArrowDown', 'ArrowUp', 'Home', 'End', 'Escape'])(
    'ignores %s when focus is in a text field',
    (key) => {
      expect(resolveKey(press(key, { target: 'typing' }), cursor())).toEqual(IGNORE);
    },
  );

  it('ignores a key in a text field even while the help overlay is open', () => {
    // The guard is checked before anything else on purpose, so no later
    // branch can reintroduce the bug by handling its own key first.
    expect(resolveKey(press('Escape', { target: 'typing' }), cursor({ helpOpen: true }))).toEqual(IGNORE);
  });
});

// --- the modifier guard -------------------------------------------------

describe('resolveKey: leaves browser and OS combinations alone', () => {
  it.each([
    ['Ctrl', { ctrlKey: true }],
    ['Cmd', { metaKey: true }],
    ['Alt', { altKey: true }],
  ])('ignores %s with a bound key', (_label, modifier) => {
    expect(resolveKey(press('j', modifier), cursor())).toEqual(IGNORE);
  });

  it('leaves Cmd+R to the browser rather than reloading the list', () => {
    expect(resolveKey(press('r', { metaKey: true }), cursor())).toEqual(IGNORE);
  });

  it('leaves Ctrl+? alone', () => {
    expect(resolveKey(press('?', { ctrlKey: true }), cursor())).toEqual(IGNORE);
  });

  it('opens help for a bare ? even though the layout produced it with Shift', () => {
    // The case the obvious guard gets wrong. Shift is not consulted at all
    // — `event.key` already carries the shifted character — so `?` arrives
    // here as `?` and survives.
    expect(resolveKey(press('?'), cursor())).toEqual<KeyAction>({ type: 'openHelp' });
  });

  it('ignores a shifted letter, because the key value itself differs', () => {
    // The other half of not consulting Shift: `J` must not reach the `j`
    // binding by accident.
    expect(resolveKey(press('J'), cursor())).toEqual(IGNORE);
  });

  it.each(['M', 'B', 'O', 'R', 'K'])('ignores %s', (key) => {
    expect(resolveKey(press(key), cursor())).toEqual(IGNORE);
  });
});

// --- movement -----------------------------------------------------------

describe('resolveKey: movement', () => {
  it('moves to the next item on j', () => {
    expect(resolveKey(press('j'), cursor({ index: 2, count: 5 }))).toEqual<KeyAction>({ type: 'move', to: 3 });
  });

  it('moves to the previous item on k', () => {
    expect(resolveKey(press('k'), cursor({ index: 2, count: 5 }))).toEqual<KeyAction>({ type: 'move', to: 1 });
  });

  it('clamps rather than wrapping when j is pressed on the last item', () => {
    // Clamped, not wrapped. Wrapping in a triage list silently returns the
    // user to the top, which reads as the list having reloaded.
    expect(resolveKey(press('j'), cursor({ index: 4, count: 5 }))).toEqual<KeyAction>({ type: 'move', to: 4 });
  });

  it('clamps rather than wrapping when k is pressed on the first item', () => {
    expect(resolveKey(press('k'), cursor({ index: 0, count: 5 }))).toEqual<KeyAction>({ type: 'move', to: 0 });
  });

  it('clamps j on the last item of a one-item list', () => {
    expect(resolveKey(press('j'), cursor({ index: 0, count: 1 }))).toEqual<KeyAction>({ type: 'move', to: 0 });
  });

  it('clamps k on the only item of a one-item list', () => {
    expect(resolveKey(press('k'), cursor({ index: 0, count: 1 }))).toEqual<KeyAction>({ type: 'move', to: 0 });
  });

  it('lands on the first item when j is pressed with no cursor yet', () => {
    expect(resolveKey(press('j'), cursor({ index: -1, onCard: false }))).toEqual<KeyAction>({ type: 'move', to: 0 });
  });

  it('lands on the first item when k is pressed with no cursor yet', () => {
    expect(resolveKey(press('k'), cursor({ index: -1, onCard: false }))).toEqual<KeyAction>({ type: 'move', to: 0 });
  });

  it('returns to the cursor rather than stepping past it when focus is off the list', () => {
    // The cursor is off screen, so the item at index 2 was never in front
    // of the user in this pass. Stepping to 3 would skip it unseen.
    expect(resolveKey(press('j'), cursor({ index: 2, count: 5, onCard: false }))).toEqual<KeyAction>({
      type: 'move',
      to: 2,
    });
  });

  it('clamps a stale off-list cursor to the end of a shorter list', () => {
    expect(resolveKey(press('j'), cursor({ index: 9, count: 3, onCard: false }))).toEqual<KeyAction>({
      type: 'move',
      to: 2,
    });
  });

  it.each(['j', 'k'])('ignores %s when the list is empty', (key) => {
    expect(resolveKey(press(key), cursor({ index: -1, count: 0, onCard: false }))).toEqual(IGNORE);
  });
});

describe('resolveKey: arrows, Home and End', () => {
  it('moves down on ArrowDown when the cursor is on a card', () => {
    expect(resolveKey(press('ArrowDown'), cursor({ index: 1, count: 5 }))).toEqual<KeyAction>({ type: 'move', to: 2 });
  });

  it('moves up on ArrowUp when the cursor is on a card', () => {
    expect(resolveKey(press('ArrowUp'), cursor({ index: 1, count: 5 }))).toEqual<KeyAction>({ type: 'move', to: 0 });
  });

  it('clamps ArrowDown at the last item', () => {
    expect(resolveKey(press('ArrowDown'), cursor({ index: 4, count: 5 }))).toEqual<KeyAction>({ type: 'move', to: 4 });
  });

  it('clamps ArrowUp at the first item', () => {
    expect(resolveKey(press('ArrowUp'), cursor({ index: 0, count: 5 }))).toEqual<KeyAction>({ type: 'move', to: 0 });
  });

  it('jumps to the first item on Home', () => {
    expect(resolveKey(press('Home'), cursor({ index: 3, count: 5 }))).toEqual<KeyAction>({ type: 'move', to: 0 });
  });

  it('jumps to the last item on End', () => {
    expect(resolveKey(press('End'), cursor({ index: 0, count: 5 }))).toEqual<KeyAction>({ type: 'move', to: 4 });
  });

  it.each(['ArrowDown', 'ArrowUp', 'Home', 'End'])(
    'leaves %s to the page when focus is not on a card',
    (key) => {
      // Off the list these keys are how the page is scrolled. Taking them
      // globally would be a regression for everyone who never presses `j`.
      expect(resolveKey(press(key), cursor({ index: 2, onCard: false }))).toEqual(IGNORE);
    },
  );
});

// --- acting on the focused item -----------------------------------------

describe('resolveKey: acting on the cursor', () => {
  it('opens on o', () => {
    expect(resolveKey(press('o'), cursor())).toEqual<KeyAction>({ type: 'open' });
  });

  it('opens on Enter when the card itself is focused', () => {
    expect(resolveKey(press('Enter', { target: 'passive' }), cursor())).toEqual<KeyAction>({ type: 'open' });
  });

  it('leaves Enter to a focused control inside the card', () => {
    // Otherwise Enter on the card's own Bookmark button would open the
    // article instead of pressing the button.
    expect(resolveKey(press('Enter', { target: 'activatable' }), cursor())).toEqual(IGNORE);
  });

  it('still opens on o when a control inside the card has focus', () => {
    // `o` is not a key any control claims, so it stays available there.
    expect(resolveKey(press('o', { target: 'activatable' }), cursor())).toEqual<KeyAction>({ type: 'open' });
  });

  it('toggles read on m', () => {
    expect(resolveKey(press('m'), cursor())).toEqual<KeyAction>({ type: 'toggleRead' });
  });

  it('toggles bookmark on b', () => {
    expect(resolveKey(press('b'), cursor())).toEqual<KeyAction>({ type: 'toggleBookmark' });
  });

  it.each(['o', 'Enter', 'm', 'b'])('ignores %s when focus is off the list', (key) => {
    // The cursor is the focus ring and nothing else, so acting on one the
    // user cannot see is how a keystroke becomes a surprise.
    expect(resolveKey(press(key), cursor({ index: 2, onCard: false }))).toEqual(IGNORE);
  });

  it.each(['o', 'Enter', 'm', 'b'])('ignores %s when the cursor has never been placed', (key) => {
    expect(resolveKey(press(key), cursor({ index: -1 }))).toEqual(IGNORE);
  });

  it.each(['o', 'Enter', 'm', 'b'])('ignores %s when the list is empty', (key) => {
    expect(resolveKey(press(key), cursor({ index: -1, count: 0 }))).toEqual(IGNORE);
  });
});

// --- reload and help ----------------------------------------------------

describe('resolveKey: reload', () => {
  it('reloads on r', () => {
    expect(resolveKey(press('r'), cursor())).toEqual<KeyAction>({ type: 'reload' });
  });

  it('reloads on r with focus off the list', () => {
    expect(resolveKey(press('r'), cursor({ index: -1, onCard: false }))).toEqual<KeyAction>({ type: 'reload' });
  });

  it('reloads on r when the list is empty', () => {
    // An empty feed is exactly when a refresh is worth asking for, so this
    // binding sits ahead of the empty-list guard.
    expect(resolveKey(press('r'), cursor({ index: -1, count: 0, onCard: false }))).toEqual<KeyAction>({
      type: 'reload',
    });
  });
});

describe('resolveKey: the help overlay', () => {
  it('opens help on ? from anywhere on the page', () => {
    expect(resolveKey(press('?'), cursor({ index: -1, count: 0, onCard: false }))).toEqual<KeyAction>({
      type: 'openHelp',
    });
  });

  it('closes help on Escape', () => {
    expect(resolveKey(press('Escape'), cursor({ helpOpen: true }))).toEqual<KeyAction>({ type: 'closeHelp' });
  });

  it('closes help on a second ?', () => {
    expect(resolveKey(press('?'), cursor({ helpOpen: true }))).toEqual<KeyAction>({ type: 'closeHelp' });
  });

  it.each(['j', 'k', 'o', 'm', 'b', 'r', 'Enter', 'ArrowDown', 'Home'])(
    'ignores %s while the overlay is open',
    (key) => {
      // The list is inert behind the dialog, so a move here would drag an
      // invisible cursor around and a toggle would change something the
      // user cannot see.
      expect(resolveKey(press(key), cursor({ helpOpen: true }))).toEqual(IGNORE);
    },
  );

  it('ignores Escape when the overlay is not open', () => {
    expect(resolveKey(press('Escape'), cursor())).toEqual(IGNORE);
  });
});

describe('resolveKey: unbound keys', () => {
  it.each(['a', 'z', '1', 'Tab', ' ', 'PageDown', 'F5'])('ignores %s', (key) => {
    expect(resolveKey(press(key), cursor())).toEqual(IGNORE);
  });

  it('leaves Tab alone so the page keeps its ordinary tab order', () => {
    expect(resolveKey(press('Tab', { target: 'activatable' }), cursor())).toEqual(IGNORE);
  });
});

// --- cursor stability ---------------------------------------------------

describe('resolveCursor', () => {
  it('keeps the cursor on its item when the list only grew', () => {
    // The infinite-scroll case: ids are appended, so nothing moves.
    expect(resolveCursor({ id: 20, index: 1 }, [10, 20, 30, 40])).toBe(20);
  });

  it('keeps the cursor on its item even when it moved position', () => {
    expect(resolveCursor({ id: 20, index: 1 }, [5, 7, 10, 20])).toBe(20);
  });

  it('takes the item that filled the gap when the cursor item is removed', () => {
    // Un-bookmarking the focused row. The row below slides up into the same
    // position, which is where the user is looking.
    expect(resolveCursor({ id: 20, index: 1 }, [10, 30, 40])).toBe(30);
  });

  it('clamps to the last item when the cursor was at the end', () => {
    expect(resolveCursor({ id: 40, index: 3 }, [10, 20, 30])).toBe(30);
  });

  it('clamps to the last item when the list shrank sharply', () => {
    expect(resolveCursor({ id: 99, index: 40 }, [10, 20])).toBe(20);
  });

  it('clamps to the first item for a negative remembered position', () => {
    expect(resolveCursor({ id: 99, index: -1 }, [10, 20])).toBe(10);
  });

  it('returns null when the list is empty', () => {
    // A reload empties the list before it refills it. There is nothing to
    // sit on, so the caller has to put focus somewhere real instead.
    expect(resolveCursor({ id: 20, index: 1 }, [])).toBeNull();
  });

  it('returns null when there was no cursor to begin with', () => {
    expect(resolveCursor(null, [10, 20])).toBeNull();
  });
});

describe('tabStopId', () => {
  it('puts the tab stop on the cursor', () => {
    expect(tabStopId(20, [10, 20, 30])).toBe(20);
  });

  it('falls back to the first item before the cursor exists', () => {
    // Without this the list has no tab stop at all and Tab skips the whole
    // thing, which would leave a keyboard user no way in.
    expect(tabStopId(null, [10, 20, 30])).toBe(10);
  });

  it('falls back to the first item when the cursor item has gone', () => {
    expect(tabStopId(99, [10, 20, 30])).toBe(10);
  });

  it('has no tab stop for an empty list', () => {
    expect(tabStopId(null, [])).toBeNull();
  });

  it('has no tab stop for an empty list even with a stale cursor', () => {
    expect(tabStopId(20, [])).toBeNull();
  });
});

// --- the overlay cannot describe bindings that do not exist -------------

/**
 * Written as a `Record` over the action union rather than a list, so adding
 * an action to `KeyAction` fails the typecheck here until it is either
 * documented or explicitly excluded. A help overlay that has quietly
 * drifted from the bindings is worse than no overlay: it is a promise the
 * page no longer keeps.
 */
const DOCUMENTED: Record<Exclude<KeyAction['type'], 'ignore'>, true> = {
  move: true,
  open: true,
  toggleRead: true,
  toggleBookmark: true,
  reload: true,
  openHelp: true,
  closeHelp: true,
};

function actionsFor(key: string): KeyAction['type'][] {
  return [cursor(), cursor({ helpOpen: true })].map((state) => resolveKey(press(key), state).type);
}

describe('SHORTCUT_HELP', () => {
  it.each(SHORTCUT_HELP.flatMap((row) => row.keys.map((key) => [key, row.action] as const)))(
    'documents %s as an actual binding for %s',
    (key, action) => {
      expect(actionsFor(key)).toContain(action);
    },
  );

  it('documents every action a binding can produce', () => {
    const documented = new Set(SHORTCUT_HELP.map((row) => row.action));
    expect([...Object.keys(DOCUMENTED)].filter((action) => !documented.has(action as never))).toEqual([]);
  });

  it('gives every row at least one key and a description', () => {
    for (const row of SHORTCUT_HELP) {
      expect(row.keys.length).toBeGreaterThan(0);
      expect(row.description.length).toBeGreaterThan(0);
    }
  });
});

describe('keyLabel', () => {
  it.each([
    ['ArrowDown', '↓'],
    ['ArrowUp', '↑'],
    ['Escape', 'Esc'],
  ])('draws %s as %s', (key, label) => {
    expect(keyLabel(key)).toBe(label);
  });

  it.each(['j', 'k', 'o', 'm', 'b', 'r', '?', 'Enter', 'Home', 'End'])(
    'draws %s as itself',
    (key) => {
      expect(keyLabel(key)).toBe(key);
    },
  );

  it('does not pick up inherited object properties', () => {
    // `toString` is on Object.prototype; a plain index lookup would return
    // a function here and render something absurd on a <kbd>.
    expect(keyLabel('toString')).toBe('toString');
  });
});
