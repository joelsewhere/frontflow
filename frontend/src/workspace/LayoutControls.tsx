/**
 * The layout controls in a workspace header.
 *
 * Three tiers decide what someone sees — their own arrangement, the
 * author's, and the one the DSL declares — and this is where the two
 * that a person can change are managed.
 *
 * Everyone gets "Reset layout", and only when they have actually
 * customised something: offering to reset what you never changed reads
 * as a broken button.
 *
 * An author additionally gets a band switcher and "Save for everyone".
 * Those are deliberately separate from dragging: moving a panel saves
 * to YOUR layout, and publishing it to everyone is a second, explicit
 * act. "I moved a panel" and "everyone should see it this way" are
 * different intentions and should not share a gesture.
 */

import { bandLabel } from "./reconcile";

export function LayoutControls({
  bands,
  breakpoints,
  band,
  editingBand,
  canAuthor,
  customised,
  onEditBand,
  onSaveForEveryone,
  onReset,
}: {
  bands: number[];
  breakpoints: number[];
  band: number;
  editingBand: number | null;
  canAuthor: boolean;
  customised: boolean;
  onEditBand: (band: number | null) => void;
  onSaveForEveryone: () => void;
  onReset: (forEveryone: boolean) => void;
}) {
  const chip =
    "border border-border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider";

  return (
    <div className="flex items-center gap-2">
      {customised && (
        <button
          type="button"
          onClick={() => onReset(false)}
          className={`${chip} text-muted hover:text-accent`}
          title="Discard your arrangement and go back to the author's"
        >
          Reset layout
        </button>
      )}

      {canAuthor && bands.length > 1 && (
        <div className="flex items-center gap-1">
          <span className="font-mono text-[10px] uppercase tracking-wider text-muted">
            Width
          </span>
          {bands.map((b) => (
            <button
              key={b}
              type="button"
              // Clicking the band already being edited leaves edit mode,
              // so the canvas goes back to following the window.
              onClick={() => onEditBand(editingBand === b ? null : b)}
              className={`${chip} ${
                editingBand === b
                  ? "border-accent text-accent"
                  : b === band
                    ? "text-ink"
                    : "text-muted hover:text-accent"
              }`}
              title={bandLabel(b, breakpoints)}
            >
              {b === 0 ? "base" : `${b}`}
            </button>
          ))}
        </div>
      )}

      {canAuthor && (
        <>
          <button
            type="button"
            onClick={onSaveForEveryone}
            className={`${chip} text-muted hover:text-accent`}
            title={`Make this arrangement the default for ${bandLabel(
              band,
              breakpoints,
            )}`}
          >
            Save for everyone
          </button>
          <button
            type="button"
            onClick={() => onReset(true)}
            className={`${chip} text-muted hover:text-error`}
            title="Drop the shared arrangement and fall back to the DSL"
          >
            Clear shared
          </button>
        </>
      )}
    </div>
  );
}
