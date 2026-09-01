import { useEffect, useId, useRef } from 'react';

import { keyLabel, SHORTCUT_HELP } from './shortcuts';
import type { ListKeyboard } from './useListKeyboard';

/**
 * Everything the keyboard layer has to render: the live region that tells a
 * screen-reader user what a keystroke just did, and the `?` overlay.
 *
 * Both pages that carry a card list mount this once, next to their list.
 */
export function KeyboardLayer({ keyboard }: { keyboard: ListKeyboard }) {
  return (
    <>
      {/*
        The card's own state — the "Read" flag, `aria-pressed` on its
        buttons — changes silently when the change came from a shortcut,
        because focus is on the article rather than on the control that
        moved. This is the only feedback a screen-reader user gets for `m`
        and `b`, and it is deliberately terse: the card is about to be
        re-read anyway, so a sentence here would be said twice.
      */}
      <p className="visually-hidden" role="status">
        {keyboard.announcement}
      </p>
      <ShortcutHelpDialog
        open={keyboard.helpOpen}
        onClosed={keyboard.handleHelpClosed}
        onDismiss={keyboard.closeHelp}
      />
    </>
  );
}

/** The visible way in, for everyone who will never guess that `?` does anything. */
export function ShortcutHelpButton({ onOpen }: { onOpen: () => void }) {
  return (
    <button type="button" className="button button--quiet shortcuts__open" onClick={onOpen}>
      Keyboard shortcuts <kbd>?</kbd>
    </button>
  );
}

interface ShortcutHelpDialogProps {
  open: boolean;
  /** The dialog's own `close` event, whatever it was that shut it. */
  onClosed: () => void;
  onDismiss: () => void;
}

/**
 * A native `<dialog>` opened with `showModal()`, so the platform supplies
 * the things a hand-rolled overlay gets wrong: `role="dialog"` and modal
 * semantics without writing either, Escape that always works, the rest of
 * the page inert while it is up, and focus containment that is a
 * containment rather than a trap — closing it is one key away and every
 * route out ends at the same `close` event, which is where focus is
 * restored.
 */
function ShortcutHelpDialog({ open, onClosed, onDismiss }: ShortcutHelpDialogProps) {
  const headingId = useId();
  const dialogRef = useRef<HTMLDialogElement | null>(null);

  useEffect(() => {
    const node = dialogRef.current;
    if (node === null) return;
    if (open && !node.open) node.showModal();
    else if (!open && node.open) node.close();
  }, [open]);

  return (
    <dialog className="shortcuts" ref={dialogRef} aria-labelledby={headingId} onClose={onClosed}>
      <h2 className="shortcuts__title" id={headingId}>
        Keyboard shortcuts
      </h2>

      <dl className="shortcuts__list">
        {SHORTCUT_HELP.map((row) => (
          <div className="shortcuts__row" key={row.description}>
            <dt>
              {row.keys.map((key) => (
                <kbd key={key}>{keyLabel(key)}</kbd>
              ))}
            </dt>
            <dd>{row.description}</dd>
          </div>
        ))}
      </dl>

      <p className="shortcuts__note">
        Shortcuts are off while you are typing in a field, and anything held with Ctrl, Cmd, or Alt
        belongs to the browser.
      </p>

      <button type="button" className="button" onClick={onDismiss}>
        Close
      </button>
    </dialog>
  );
}
