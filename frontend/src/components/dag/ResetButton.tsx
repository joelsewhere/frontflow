import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { clearSubmission, type ClearResponse } from "../../lib/api";
import { Modal } from "../ui/Modal";

interface ResetButtonProps {
  formId: string;
  submissionId: string;
  /**
   * Task to clear from (inclusive). Omit for a full-run clear. The
   * backend cascades downstream automatically.
   */
  fromTaskId?: string;
  /** How the trigger button looks. Choose based on context. */
  variant: "icon" | "link" | "button";
  /** When true, the dialog offers an Edit option (re-open keeping the
   *  step's answers) alongside Reset. Only meaningful per-node. */
  allowEdit?: boolean;
  /** When true, a plain reset offers a scope choice: clear this step
   *  and its dependents (cascade), or rerun only this step (node-only).
   *  For rerunning a finished/failed operator or backend step. */
  allowScopeChoice?: boolean;
  /** Override the default trigger label (defaults vary by variant). */
  label?: string;
}

/**
 * Self-contained reset action. Renders a trigger styled per variant;
 * clicking opens a confirmation dialog that:
 *
 *   1. Fires a dry-run clear to preview affected tasks.
 *   2. Shows that preview to the user.
 *   3. On confirm, fires the real clear and invalidates query caches.
 *
 * Encapsulating the full flow keeps the call site small — every place
 * that needs a reset (ProgressNode tasks, HitlNode submitted, the
 * completion node) just drops in one `<ResetButton>` with the right
 * variant.
 */
export function ResetButton({
  formId,
  submissionId,
  fromTaskId,
  variant,
  allowEdit,
  allowScopeChoice,
  label,
}: ResetButtonProps) {
  const [open, setOpen] = useState(false);
  const trigger = renderTrigger(
    variant,
    label ?? defaultLabel(variant, allowEdit),
    () => setOpen(true),
  );

  return (
    <>
      {trigger}
      <ResetDialog
        open={open}
        onClose={() => setOpen(false)}
        formId={formId}
        submissionId={submissionId}
        fromTaskId={fromTaskId}
        allowEdit={allowEdit}
        allowScopeChoice={allowScopeChoice}
      />
    </>
  );
}

function defaultLabel(
  variant: ResetButtonProps["variant"],
  allowEdit?: boolean,
): string {
  switch (variant) {
    case "icon":
      return "Reset";
    case "link":
      return allowEdit ? "Edit" : "Reset from here";
    case "button":
      return "Reset run";
  }
}

function renderTrigger(
  variant: ResetButtonProps["variant"],
  label: string,
  onClick: () => void,
) {
  switch (variant) {
    case "icon":
      return (
        <button
          type="button"
          onClick={onClick}
          aria-label={label}
          title={label}
          className="text-muted hover:text-ink transition-colors p-1 -m-1 inline-flex items-center"
        >
          <ResetIcon />
        </button>
      );
    case "link":
      return (
        <button
          type="button"
          onClick={onClick}
          className="text-[10px] uppercase tracking-[0.2em] text-muted hover:text-ink underline underline-offset-2"
        >
          {label}
        </button>
      );
    case "button":
      return (
        <button
          type="button"
          onClick={onClick}
          className="self-start inline-flex items-center gap-2 px-5 py-3 bg-surface text-ink border border-border font-sans text-sm uppercase tracking-[0.18em] transition-colors hover:border-ink"
        >
          <ResetIcon />
          <span>{label}</span>
        </button>
      );
  }
}

function ResetIcon() {
  // Counter-clockwise circular arrow.
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M3 8a5 5 0 0 1 8.5-3.5L13 6" />
      <path d="M13 3v3h-3" />
      <path d="M13 8a5 5 0 0 1-8.5 3.5L3 10" />
      <path d="M3 13v-3h3" />
    </svg>
  );
}

// --- Dialog ----------------------------------------------------------------

interface ResetDialogProps {
  open: boolean;
  onClose: () => void;
  formId: string;
  submissionId: string;
  fromTaskId?: string;
  /** When true, the dialog offers an Edit option alongside Reset. */
  allowEdit?: boolean;
  /** When true, a plain reset offers cascade vs node-only scope. */
  allowScopeChoice?: boolean;
}

type RewindMode = "reset" | "reset_node_only" | "edit" | "edit_node_only";

function ResetDialog({
  open,
  onClose,
  formId,
  submissionId,
  fromTaskId,
  allowEdit,
  allowScopeChoice,
}: ResetDialogProps) {
  const queryClient = useQueryClient();
  const [preview, setPreview] = useState<ClearResponse | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [isConfirming, setIsConfirming] = useState(false);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  // Reset is the default — the established, non-surprising action.
  const [mode, setMode] = useState<RewindMode>("reset");

  // Reset state whenever the dialog opens; fetch the dry-run preview.
  // The preview's affected set is meaningful for Reset (which truncates
  // downstream); for the edit modes the real impact depends on what the
  // user changes, so the dialog describes behavior instead of listing.
  useEffect(() => {
    if (!open) {
      setPreview(null);
      setPreviewError(null);
      setConfirmError(null);
      setIsConfirming(false);
      setMode("reset");
      return;
    }
    let cancelled = false;
    clearSubmission(formId, submissionId, {
      from_task_id: fromTaskId,
      dry_run: true,
    })
      .then((res) => {
        if (!cancelled) setPreview(res);
      })
      .catch((err) => {
        if (!cancelled) {
          setPreviewError(err instanceof Error ? err.message : String(err));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [open, formId, submissionId, fromTaskId]);

  const isEdit = mode === "edit" || mode === "edit_node_only";
  const isNodeOnly = mode === "reset_node_only" || mode === "edit_node_only";

  const handleConfirm = async () => {
    setIsConfirming(true);
    setConfirmError(null);
    try {
      await clearSubmission(formId, submissionId, {
        from_task_id: fromTaskId,
        dry_run: false,
        mode: isEdit ? "edit" : "reset",
        scope: isNodeOnly ? "node_only" : "cascade",
      });
      // Invalidate both the submission and any cached step details.
      queryClient.invalidateQueries({
        queryKey: ["submission", formId, submissionId],
      });
      queryClient.invalidateQueries({
        queryKey: ["stepDetail", formId, submissionId],
      });
      onClose();
    } catch (err) {
      setConfirmError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsConfirming(false);
    }
  };

  const affectedCount = preview?.affected_tasks.length ?? 0;
  const title = allowEdit
    ? fromTaskId
      ? `Revisit ${fromTaskId}`
      : "Revisit run"
    : fromTaskId
      ? `Reset from ${fromTaskId}`
      : "Reset entire run";

  return (
    <Modal open={open} onClose={onClose} preventDismiss={isConfirming}>
      <div className="flex flex-col gap-4">
        <h2 className="font-display text-base uppercase tracking-[0.18em] text-ink">
          {title}
        </h2>

        {allowEdit ? (
          <div className="flex flex-col gap-2">
            <ModeOption
              selected={mode === "reset"}
              onSelect={() => setMode("reset")}
              title="Reset"
              description="Clear this step and everything after it; start over."
            />
            <ModeOption
              selected={mode === "edit"}
              onSelect={() => setMode("edit")}
              title="Edit"
              description="Keep your answers and adjust them. Steps that depend on what you change are re-opened; the rest stay as they are."
            />
            <ModeOption
              selected={mode === "edit_node_only"}
              onSelect={() => setMode("edit_node_only")}
              title="Edit this step only"
              description="Keep your answers and adjust them. Downstream steps are left exactly as they are — you decide whether they still fit."
            />
          </div>
        ) : allowScopeChoice ? (
          <div className="flex flex-col gap-2">
            <ModeOption
              selected={mode === "reset"}
              onSelect={() => setMode("reset")}
              title="Reset and run dependents"
              description="Rerun this step, then clear and rerun every step that depends on it."
            />
            <ModeOption
              selected={mode === "reset_node_only"}
              onSelect={() => setMode("reset_node_only")}
              title="Run only this step"
              description="Rerun just this step. Downstream steps are left exactly as they are — you decide whether they still fit."
            />
          </div>
        ) : null}

        {previewError ? (
          <p className="text-sm text-error">{previewError}</p>
        ) : mode === "reset_node_only" ? (
          <p className="text-sm text-muted leading-relaxed">
            This reruns {fromTaskId ?? "this step"} on its own. Steps
            after it are left exactly as they are — nothing downstream
            is cleared.
          </p>
        ) : isEdit ? (
          <p className="text-sm text-muted leading-relaxed">
            {mode === "edit"
              ? `This re-opens ${fromTaskId ?? "the step"} with your answers. When you submit, the steps after it are checked against what you changed and only the affected ones are re-opened.`
              : `This re-opens ${fromTaskId ?? "the step"} with your answers. Nothing downstream is touched — submit when you're done, or cancel to leave it unchanged.`}
          </p>
        ) : !preview ? (
          <p className="text-sm text-muted">Loading preview…</p>
        ) : affectedCount === 0 ? (
          <p className="text-sm text-muted">
            Nothing to change — no tasks have entered this run yet.
          </p>
        ) : (
          <>
            <p className="text-sm text-muted leading-relaxed">
              This clears {affectedCount}{" "}
              {affectedCount === 1 ? "step" : "steps"} and restarts the
              run from this point. The run resumes automatically.
            </p>
            <ul className="bg-bg border border-border px-4 py-3 font-mono text-xs flex flex-col gap-1">
              {preview.affected_tasks.map((tid) => (
                <li key={tid} className="text-ink">
                  {tid}
                </li>
              ))}
            </ul>
          </>
        )}

        {confirmError ? (
          <p className="text-sm text-error">{confirmError}</p>
        ) : null}

        <div className="flex justify-end gap-3 pt-1">
          <button
            type="button"
            onClick={onClose}
            disabled={isConfirming}
            className="px-4 py-2 text-sm uppercase tracking-[0.18em] text-muted hover:text-ink disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={
              isConfirming ||
              (mode === "reset" && (!preview || affectedCount === 0))
            }
            className="px-5 py-2 bg-ink text-bg text-sm uppercase tracking-[0.18em] hover:bg-accent-hover disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isConfirming
              ? isEdit
                ? "Opening…"
                : "Rerunning…"
              : isEdit
                ? "Edit"
                : mode === "reset_node_only"
                  ? "Rerun step"
                  : "Reset"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

/** One selectable mode in the reset/edit chooser. */
function ModeOption({
  selected,
  onSelect,
  title,
  description,
}: {
  selected: boolean;
  onSelect: () => void;
  title: string;
  description: string;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={[
        "flex items-start gap-3 text-left px-4 py-3 border transition-colors",
        selected
          ? "border-ink bg-surface"
          : "border-border hover:border-muted",
      ].join(" ")}
    >
      <span
        aria-hidden
        className={[
          "mt-0.5 h-3 w-3 border shrink-0",
          selected ? "border-ink bg-ink" : "border-muted",
        ].join(" ")}
      />
      <span>
        <span className="block font-sans text-sm uppercase tracking-[0.16em] text-ink">
          {title}
        </span>
        <span className="block text-xs text-muted mt-0.5">
          {description}
        </span>
      </span>
    </button>
  );
}
