import { type RefObject, useCallback, useEffect, useRef, useState } from 'react';

import {
  classifyTarget,
  type CursorAnchor,
  type KeyAction,
  resolveCursor,
  resolveKey,
  tabStopId,
} from './shortcuts';

/**
 * The effects half of the keyboard layer. Everything that decides lives in
 * `shortcuts.ts`; what is left here is the part that cannot run without a
 * DOM, and it is kept small for exactly that reason — it finds nodes, calls
 * `.focus()`, and drives a `<dialog>`.
 *
 * Focus is real throughout. There is no parallel "selected index" painted
 * in CSS: the cursor is a `.focus()` on the card's `<article>`, so the
 * browser scrolls it into view, a screen reader announces it, and
 * `:focus-visible` draws it without being asked. Roving tabindex keeps the
 * list to a single tab stop, so Tab still reaches it in one press and still
 * leaves in one.
 *
 * The container ref is passed in rather than created and handed back. A
 * hook that returns a ref makes every other field of its return value a ref
 * read during render as far as `react-hooks/refs` is concerned, and the
 * rule is right to say so — nothing rendering needs `.current`.
 */

export interface ListKeyboardHandlers {
  /**
   * Each does the work and returns what to announce, or null for silence.
   * Feedback is the caller's business, because it is the caller that knows
   * whether `m` has just marked an item read or unread.
   */
  onToggleRead: (id: number) => string | null;
  onToggleBookmark: (id: number) => string | null;
  onReload: () => string | null;
}

export interface ListKeyboardOptions {
  /** Item ids in display order. The cursor is an id, not a position. */
  ids: number[];
  /**
   * The element wrapping the list. It must carry `tabIndex={-1}`: when the
   * card holding focus is removed and the list has nothing left to sit on,
   * this is where focus lands, and the alternative is `<body>`.
   */
  containerRef: RefObject<HTMLDivElement | null>;
  handlers: ListKeyboardHandlers;
}

export interface ListKeyboard {
  /** The one card in the tab order, or null when the list is empty. */
  tabStop: number | null;
  /** Announced politely after a keyboard-driven change. */
  announcement: string;
  helpOpen: boolean;
  openHelp: () => void;
  closeHelp: () => void;
  /** Wired to the dialog's own `close` event, whatever it was that shut it. */
  handleHelpClosed: () => void;
}

/** Silence is a legitimate outcome, so only a real message reaches the region. */
function announce(message: string | null, publish: (value: string) => void) {
  if (message !== null) publish(message);
}

export function useListKeyboard({ ids, containerRef, handlers }: ListKeyboardOptions): ListKeyboard {
  const [cursorId, setCursorId] = useState<number | null>(null);
  const [announcement, setAnnouncement] = useState('');
  const [helpOpen, setHelpOpen] = useState(false);

  /** Where the cursor is, by id and by position; see `resolveCursor`. */
  const cursorRef = useRef<CursorAnchor | null>(null);
  /** Whether focus is inside a card right now, rather than merely having been. */
  const onCardRef = useRef(false);
  const helpReturnRef = useRef<HTMLElement | null>(null);
  /**
   * Mirrors `helpOpen` for the key handler, and is written in the same
   * breath as the state rather than in an effect: a keystroke arriving
   * between a render and its effects would otherwise be resolved against an
   * overlay that is already open.
   */
  const helpOpenRef = useRef(false);
  const seenIdsRef = useRef('');

  // The shape `usePagedResource` already uses here: the listeners below are
  // registered once and read current values through refs, so a fresh
  // handlers object on every render does not tear down and rebuild a
  // document-level key listener.
  const idsRef = useRef(ids);
  const handlersRef = useRef(handlers);
  useEffect(() => {
    idsRef.current = ids;
    handlersRef.current = handlers;
  });

  const focusCard = useCallback(
    (id: number): boolean => {
      const node = containerRef.current?.querySelector<HTMLElement>(
        `[data-card-id="${String(id)}"]`,
      );
      if (!node) return false;
      node.focus();
      return true;
    },
    [containerRef],
  );

  /**
   * One `focusin` listener does two jobs, because they are answers to the
   * same question. It syncs the cursor whenever focus arrives on a card by
   * any route — Tab, a click, a shortcut — so `j` continues from where the
   * user actually is; and it records whether focus is on a card at all,
   * which is what tells the list-change effect below whether a focus it
   * finds missing is one this layer destroyed or one the user had already
   * moved elsewhere.
   */
  useEffect(() => {
    const onFocusIn = (event: FocusEvent) => {
      const container = containerRef.current;
      const target = event.target;
      const card =
        container !== null && target instanceof Element && container.contains(target)
          ? target.closest('[data-card-id]')
          : null;
      if (card === null) {
        onCardRef.current = false;
        return;
      }
      const raw = card.getAttribute('data-card-id');
      const id = raw === null ? Number.NaN : Number(raw);
      if (!Number.isInteger(id)) {
        onCardRef.current = false;
        return;
      }
      onCardRef.current = true;
      cursorRef.current = { id, index: idsRef.current.indexOf(id) };
      setCursorId(id);
    };
    document.addEventListener('focusin', onFocusIn);
    return () => {
      document.removeEventListener('focusin', onFocusIn);
    };
  }, [containerRef]);

  /**
   * Cursor stability. The list moves under the cursor for three reasons —
   * an infinite-scroll append, a `reload()`, and a row removed outright by
   * un-bookmarking it — and only the last two can take the cursor's item
   * with them.
   *
   * Focus is chased only when this layer is what destroyed it. If the user
   * has already moved to the filter bar, dragging them back would itself be
   * the rug-pull; if the card holding focus was removed, the browser has
   * quietly dropped focus on `<body>`, and leaving it there is worse.
   */
  useEffect(() => {
    const key = ids.join(',');
    if (key === seenIdsRef.current) return;
    seenIdsRef.current = key;

    const previous = cursorRef.current;
    if (previous === null) return;
    if (ids.includes(previous.id)) {
      cursorRef.current = { id: previous.id, index: ids.indexOf(previous.id) };
      return;
    }

    const next = resolveCursor(previous, ids);
    cursorRef.current = next === null ? null : { id: next, index: ids.indexOf(next) };
    setCursorId(next);

    if (!onCardRef.current) return;
    if (next !== null && focusCard(next)) return;
    // Nothing left to sit on: a reload empties the list before it refills
    // it. This is what the container's `tabIndex={-1}` is for.
    containerRef.current?.focus();
    onCardRef.current = false;
  }, [containerRef, focusCard, ids]);

  const openHelp = useCallback(() => {
    if (helpOpenRef.current) return;
    // Captured before the overlay takes focus, which is the only moment it
    // is still there to capture.
    const active = document.activeElement;
    helpReturnRef.current = active instanceof HTMLElement ? active : null;
    helpOpenRef.current = true;
    setHelpOpen(true);
  }, []);

  const closeHelp = useCallback(() => {
    helpOpenRef.current = false;
    setHelpOpen(false);
  }, []);

  /**
   * Runs from the dialog's own `close` event, so the dialog is already shut
   * and focus can be placed without the modal pulling it straight back. The
   * browser restores focus itself, but only to an element that still
   * exists; the fallbacks are here because the list can have been reloaded,
   * or emptied, while the overlay was up.
   */
  const handleHelpClosed = useCallback(() => {
    helpOpenRef.current = false;
    setHelpOpen(false);
    const back = helpReturnRef.current;
    helpReturnRef.current = null;
    if (back?.isConnected === true) {
      back.focus();
      return;
    }
    const anchor = cursorRef.current;
    if (anchor !== null && focusCard(anchor.id)) return;
    containerRef.current?.focus();
  }, [containerRef, focusCard]);

  const run = useCallback(
    (action: KeyAction, currentIds: number[], index: number) => {
      const current = handlersRef.current;
      switch (action.type) {
        case 'move': {
          const id = currentIds[action.to];
          cursorRef.current = { id, index: action.to };
          setCursorId(id);
          focusCard(id);
          return;
        }
        case 'open':
          // Click the card's own link rather than reaching for the URL
          // again from here: it is the identical path a mouse click takes,
          // so target, rel, and the mark-as-read all stay in one place and
          // cannot drift apart. Nothing is announced — the new tab takes
          // focus, so an announcement in this one goes nowhere.
          containerRef.current
            ?.querySelector<HTMLAnchorElement>(
              `[data-card-id="${String(currentIds[index])}"] [data-card-link]`,
            )
            ?.click();
          return;
        case 'toggleRead':
          announce(current.onToggleRead(currentIds[index]), setAnnouncement);
          return;
        case 'toggleBookmark':
          announce(current.onToggleBookmark(currentIds[index]), setAnnouncement);
          return;
        case 'reload':
          announce(current.onReload(), setAnnouncement);
          return;
        case 'openHelp':
          openHelp();
          return;
        case 'closeHelp':
          closeHelp();
          return;
        case 'ignore':
          return;
      }
    },
    [closeHelp, containerRef, focusCard, openHelp],
  );

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const currentIds = idsRef.current;
      const anchor = cursorRef.current;
      const index = anchor === null ? -1 : currentIds.indexOf(anchor.id);
      const action = resolveKey(
        {
          key: event.key,
          ctrlKey: event.ctrlKey,
          metaKey: event.metaKey,
          altKey: event.altKey,
          target: classifyTarget(event.target),
        },
        {
          index,
          count: currentIds.length,
          onCard: onCardRef.current,
          helpOpen: helpOpenRef.current,
        },
      );
      if (action.type === 'ignore') return;
      // Only for a keystroke actually being consumed. The arrows and
      // Home/End scroll the page, and swallowing them when the binding did
      // nothing would be the regression.
      event.preventDefault();
      run(action, currentIds, index);
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [run]);

  return {
    tabStop: tabStopId(cursorId, ids),
    announcement,
    helpOpen,
    openHelp,
    closeHelp,
    handleHelpClosed,
  };
}
