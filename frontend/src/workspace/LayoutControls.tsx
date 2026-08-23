/**
 * The layout controls in a workspace header.
 *
 * Three tiers decide what someone sees — their own arrangement, the
 * author's, and the one the DSL declares — and this is where the two
 * that a person can change are managed.
 *
 * Everyone gets "Save layout", which keeps the arrangement for them
 * alone, and "Reset layout" once there is something to reset — offering
 * to discard what you never changed reads as a broken button. "Save
 * layout" appears only when the dock has actually been rearranged, for
 * the same reason.
 *
 * An author additionally gets a band switcher and "Save for everyone",
 * and only while develop mode is on: with it off the workspace is meant
 * to be exactly what a viewer gets, and a viewer has no such button.
 *
 * The two saves stay separate. "I want my panels here" and "everyone
 * should see it this way" are different intentions, and one gesture
 * should not do both.
 */

import { bandLabel } from "./reconcile";

export function LayoutControls({
  bands,
  breakpoints,
  band,
  editingBand,
  canAuthor,
  customised,
  dirty,
  onEditBand,
  onSave,
  onSaveForEveryone,
  onReset,
}: {
  bands: number[];
  breakpoints: number[];
  band: number;
  editingBand: number | null;
  canAuthor: boolean;
  customised: boolean;
  /** The dock has been rearranged since it was last applied or saved. */
  dirty: boolean;
  onEditBand: (band: number | null) => void;
  onSave: () => void;
  onSaveForEveryone: () => void;
  onReset: (forEveryone: boolean) => void;
}) {
  const chip =
    "border border-border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider";

  return (
    <div className="flex items-center gap-2">
      {dirty && (
        <button
          type="button"
          onClick={onSave}
          className={`${chip} border-accent text-accent hover:bg-accent hover:text-bg`}
          title="Keep this arrangement for you"
        >
          Save layout
        </button>
      )}

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
