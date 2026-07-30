import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  clearSubmission,
  refreshForm,
  repinSubmission,
  type ClearResponse,
  type RepinIssue,
} from "../../lib/api";
import { Modal } from "../ui/Modal";
import { useAuth } from "../../auth/AuthContext";
import { useSubmission } from "../../hooks/useSubmission";
import { compareVersion, formatVersion } from "../../lib/version";

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
  const { user } = useAuth();
  const submission = useSubmission(formId, submissionId).data;
  const [preview, setPreview] = useState<ClearResponse | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [isConfirming, setIsConfirming] = useState(false);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  // Reset is the default — the established, non-surprising action.
  const [mode, setMode] = useState<RewindMode>("reset");
  // "Use latest form" — admin-only, shown when the submission lags
  // the live form. Default off so the existing behavior is preserved.
  // When the regular re-pin returns 409 (shape incompatibility), the
  // dialog surfaces the issues and a "Force" toggle the admin can
  // flip to bypass the check.
  const [useLatest, setUseLatest] = useState(false);
  const [repinIssues, setRepinIssues] = useState<RepinIssue[] | null>(null);
  const [forceRepin, setForceRepin] = useState(false);
  // When the modal opens for an admin, the dialog auto-runs a reparse
  // against the form source on disk. This catches the common admin
  // workflow — "I edited the form file and want to apply it to this
  // submission" — without making the admin trigger refresh
  // separately. Three states:
  //   null         — not yet attempted (modal closed, or not admin)
  //   'pending'    — refresh in flight
  //   'done'       — refresh + refetch resolved successfully
  //   {error: ...} — refresh failed (form has a syntax error etc).
  //                  Surfaced inline so the admin sees their typo
  //                  rather than silently getting "no new version."
  const [autoRefresh, setAutoRefresh] = useState<
    null | "pending" | "done" | { error: string }
  >(null);

  // The latest-form affordance is admin-only, only meaningful when the
  // submission lags the live form. Compared on the (major, minor)
  // tuple so a minor-only difference (a body-only code edit that
  // didn't bump the compiled-graph hash) still surfaces the re-pin
  // option in the dialog — useful when auto-repin-minor is off and an
  // admin wants the live source applied to this submission directly
  // from here rather than navigating to the admin portal. If the form
  // has no live version (load error) `live_form_version` falls back
  // to `form_version` and the comparison is 0 — option stays hidden.
  const submissionLags =
    !!submission &&
    compareVersion(
      {
        major: submission.live_form_version,
        minor: submission.live_minor_version,
      },
      {
        major: submission.form_version,
        minor: submission.form_minor_version,
      },
    ) > 0;
  const showUseLatest = !!user?.is_admin && submissionLags;

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
      setUseLatest(false);
      setRepinIssues(null);
      setForceRepin(false);
      setAutoRefresh(null);
      return;
    }
    let cancelled = false;
    // Admin auto-reparse: pick up any form-source changes the admin
    // made before opening this modal. Runs in parallel with the
    // dry-run preview below; both must resolve before the confirm
    // button activates. Non-admins skip this entirely.
    if (user?.is_admin) {
      setAutoRefresh("pending");
      refreshForm(formId)
        .then((res) => {
          if (cancelled) return;
          if (res.status === "error") {
            setAutoRefresh({
              error:
                "The form's source has a problem and couldn't " +
                "be reparsed: " + (res.error ?? "unknown error"),
            });
            return;
          }
          // Reparse succeeded — invalidate the cached submission so
          // useSubmission re-fetches with the (possibly new) live
          // version. The dialog reads submission.live_form_version
          // from the refreshed payload.
          queryClient
            .invalidateQueries({
              queryKey: ["submission", formId, submissionId],
            })
            .finally(() => {
              if (!cancelled) setAutoRefresh("done");
            });
        })
        .catch((err) => {
          if (!cancelled) {
            setAutoRefresh({
              error: err instanceof Error ? err.message : String(err),
            });
          }
        });
    }
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
  }, [
    open, formId, submissionId, fromTaskId,
    user?.is_admin, queryClient,
  ]);

  const isEdit = mode === "edit" || mode === "edit_node_only";
  const isNodeOnly = mode === "reset_node_only" || mode === "edit_node_only";

  const handleConfirm = async () => {
    setIsConfirming(true);
    setConfirmError(null);
    try {
      // "Use latest form" — three steps in order, abort on the first
      // failure so we don't do partial work the user didn't expect:
      //   1. Reparse the form (picks up source changes from disk).
      //   2. Re-pin this submission to the new live version. On a
      //      shape-incompatibility the response carries `repinned:
      //      false` and a non-empty `issues` array; the dialog
      //      surfaces those and offers a Force toggle to bypass.
      //   3. Apply the chosen reset/edit mode against the new pin.
      if (useLatest) {
        // (1) Reparse. A form with a syntax error returns `status:
        // "error"`; refuse to proceed — repin against a broken form
        // would either pin to a stale version or 404.
        const refresh = await refreshForm(formId);
        if (refresh.status === "error") {
          setConfirmError(
            "The form's source has a problem and couldn't be " +
              "reparsed: " + (refresh.error ?? "unknown error"),
          );
          return;
        }
        // (2) Re-pin. Pass `force` when the admin opted in after a
        // previous attempt surfaced incompatibility issues.
        const repin = await repinSubmission(formId, submissionId, {
          force: forceRepin,
        });
        if (!repin.repinned) {
          // Compatibility issues. Show them in the dialog and let
          // the admin flip Force to proceed; do NOT run the clear.
          setRepinIssues(repin.issues);
          return;
        }
        // Re-pin succeeded; clear the stashed issues so the dialog
        // no longer shows the Force prompt next time around.
        setRepinIssues(null);
      }
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

        {autoRefresh === "pending" ? (
          <p className="text-xs text-muted">
            Checking for form updates…
          </p>
        ) : autoRefresh && typeof autoRefresh === "object" ? (
          <div className="border border-warning bg-bg px-3 py-2">
            <p className="text-xs text-warning">
              {autoRefresh.error}
            </p>
            <p className="text-xs text-muted mt-1">
              The action below still runs against the currently
              loaded version of the form.
            </p>
          </div>
        ) : null}

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

        {showUseLatest ? (
          <UseLatestForm
            checked={useLatest}
            onToggle={() => {
              setUseLatest((v) => !v);
              setRepinIssues(null);
              setForceRepin(false);
            }}
            fromVersion={submission!.form_version}
            fromMinorVersion={submission!.form_minor_version}
            toVersion={submission!.live_form_version}
            toMinorVersion={submission!.live_minor_version}
            issues={repinIssues}
            forceRepin={forceRepin}
            onForceToggle={() => setForceRepin((v) => !v)}
          />
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
              autoRefresh === "pending" ||
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


/** Admin-only inline affordance for "reparse the form then re-pin
 *  this submission to the new live version, before applying the
 *  selected reset/edit mode". Reduces a 5-click admin task to one
 *  checkbox in the modal the admin is already in.
 *
 *  When the re-pin runs into shape incompatibilities (a deleted
 *  field, a renamed node, a type change), the server returns a 200
 *  with `repinned: false` and a diff. The component renders the
 *  diff and exposes a `Force` toggle the admin can flip to bypass
 *  the check — the existing chain becomes read-only history and a
 *  fresh empty chain starts at the live version. */
function UseLatestForm({
  checked,
  onToggle,
  fromVersion,
  fromMinorVersion,
  toVersion,
  toMinorVersion,
  issues,
  forceRepin,
  onForceToggle,
}: {
  checked: boolean;
  onToggle: () => void;
  fromVersion: number;
  fromMinorVersion: number;
  toVersion: number;
  toMinorVersion: number;
  issues: RepinIssue[] | null;
  forceRepin: boolean;
  onForceToggle: () => void;
}) {
  const fromLabel = formatVersion(fromVersion, fromMinorVersion);
  const toLabel = formatVersion(toVersion, toMinorVersion);
  return (
    <div className="flex flex-col gap-2 border border-border bg-surface px-4 py-3">
      <label className="flex items-start gap-3 text-left cursor-pointer">
        <input
          type="checkbox"
          checked={checked}
          onChange={onToggle}
          className="mt-1 h-3 w-3 cursor-pointer accent-ink"
        />
        <span>
          <span className="block font-sans text-sm uppercase tracking-[0.16em] text-ink">
            Use latest form ({toLabel})
          </span>
          <span className="block text-xs text-muted mt-0.5">
            Reparse this form and re-pin the submission from{" "}
            {fromLabel} to {toLabel} before applying the action above.
            Admin-only.
          </span>
        </span>
      </label>

      {issues && issues.length > 0 ? (
        <div className="mt-1 flex flex-col gap-2 border border-warning bg-bg px-3 py-2">
          <p className="text-xs text-warning">
            The submission's shape is incompatible with {toLabel}:
          </p>
          <ul className="list-disc pl-4 text-xs text-ink">
            {issues.map((iss, i) => (
              <li key={i}>
                <span className="font-mono">{iss.kind}</span>
                {iss.node_id ? (
                  <> on <span className="font-mono">{iss.node_id}</span></>
                ) : null}
                {iss.detail ? <> — {iss.detail}</> : null}
              </li>
            ))}
          </ul>
          <label className="mt-1 flex items-start gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={forceRepin}
              onChange={onForceToggle}
              className="mt-0.5 h-3 w-3 cursor-pointer accent-ink"
            />
            <span className="text-xs text-ink">
              Force re-pin — freeze the current chain as history and
              start a fresh empty chain on {toLabel}. The current
              answers will not be carried forward.
            </span>
          </label>
        </div>
      ) : null}
    </div>
  );
}
