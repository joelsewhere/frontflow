import { useCallback, useRef, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  ApiError,
  type AssignedChild,
  type EventRow,
  type RepinIssue,
  type StepDetailRow,
  type SubmissionDetail,
  type VersionOption,
  repinSubmission,
} from "../../lib/api";
import { formatTimestamp } from "../../lib/format";
import { formatVersion, compareVersion } from "../../lib/version";
import { useSubmissionDetail } from "../../hooks/useSubmissionDetail";
import { useAuth } from "../../auth/AuthContext";
import { StatePill } from "../listing/StatePill";
import { Modal } from "../ui/Modal";
import { SubmissionGraph } from "./SubmissionGraph";

/**
 * The submission's persisted record — state, version, steps with
 * captured values, and event history. Rendered inside the
 * `SubmissionDrawer` on the form admin page; also reachable as a
 * standalone surface for compatibility.
 *
 * Width-flexible: works in a ~560px panel or a wide page layout.
 */
export function SubmissionSummaryContent({
  formId,
  submissionId,
}: {
  formId: string;
  submissionId: string;
}) {
  // The version_id the user has picked in the history toggle.
  // undefined = view the active chain (default).
  const [pickedVersionId, setPickedVersionId] = useState<number | undefined>(
    undefined,
  );
  const { data: detail, error, isLoading, refetch } = useSubmissionDetail(
    formId,
    submissionId,
    pickedVersionId,
  );
  const { user } = useAuth();

  // Steps-section view mode — URL-backed so the page state is
  // shareable (same rule as the Reports tab). Default `graph` is
  // omitted from the URL; an explicit `?tab=list` switches back to
  // the flat list view.
  const [searchParams, setSearchParams] = useSearchParams();
  const tab: "graph" | "list" =
    searchParams.get("tab") === "list" ? "list" : "graph";
  const setTab = useCallback(
    (next: "graph" | "list") => {
      const p = new URLSearchParams(searchParams);
      if (next === "graph") p.delete("tab");
      else p.set("tab", "list");
      setSearchParams(p, { replace: false });
    },
    [searchParams, setSearchParams],
  );

  // Scroll-to-step navigation from a graph node click. The graph
  // calls `onNodeClick(nodeId)`; we switch to the list tab if not
  // already there, then scroll the matching step block into view
  // and briefly highlight it. Block ids are `step-block-<seq>`,
  // attached via ref-map below.
  const stepBlockRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  const [flashStep, setFlashStep] = useState<string | null>(null);
  const flashTimer = useRef<number | null>(null);
  const handleGraphNodeClick = useCallback(
    (nodeId: string) => {
      // Always switch to list — the graph itself has no step detail
      // beyond what it shows, so jumping to the list is what makes
      // the graph a navigation tool rather than a static viz.
      setTab("list");
      // Defer scrolling until after the tab swap renders the list.
      window.setTimeout(() => {
        const el = stepBlockRefs.current.get(nodeId);
        if (el) {
          el.scrollIntoView({ behavior: "smooth", block: "center" });
          setFlashStep(nodeId);
          if (flashTimer.current) window.clearTimeout(flashTimer.current);
          flashTimer.current = window.setTimeout(
            () => setFlashStep(null),
            1500,
          );
        }
      }, 0);
    },
    [setTab],
  );

  // Re-pin only makes sense when the user is on the *active* chain —
  // history views are read-only. Minor-only deltas count as a repin
  // opportunity too; compareVersion encodes the (major, minor) tuple
  // rule once so it doesn't drift from the rest of the UI.
  const canRepin =
    !!user?.is_admin &&
    !!detail &&
    detail.is_viewing_active &&
    compareVersion(
      {
        major: detail.live_form_version,
        minor: detail.live_minor_version,
      },
      {
        major: detail.form_version,
        minor: detail.form_minor_version,
      },
    ) > 0;

  if (isLoading) {
    return (
      <p className="font-display text-2xl text-ink opacity-30">Loading…</p>
    );
  }
  if (error) {
    return (
      <div className="border border-error bg-surface p-6">
        <p className="text-error text-sm">
          {error instanceof ApiError && error.status === 404
            ? "This submission doesn't exist."
            : `Couldn't load this submission: ${
                error instanceof ApiError ? error.message : "unknown error"
              }`}
        </p>
      </div>
    );
  }
  if (!detail) return null;

  const hasMultipleVersions = detail.available_versions.length > 1;

  return (
    <div className="flex flex-col gap-6">
      <header>
        <p className="font-mono text-[11px] uppercase tracking-[0.25em] text-muted">
          Submission
        </p>
        <h2 className="mt-1 break-all font-mono text-xl font-semibold text-ink">
          {detail.submission_id ?? detail.handle}
        </h2>
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <StatePill state={detail.state} />
          <span className="font-mono text-xs text-muted">
            version{" "}
            {formatVersion(detail.form_version, detail.form_minor_version)}
            {!detail.is_viewing_active ? (
              <span className="ml-1 text-muted">
                (viewing frozen{" "}
                {formatVersion(
                  detail.viewing_version,
                  detail.viewing_minor_version,
                )}
                )
              </span>
            ) : compareVersion(
                {
                  major: detail.live_form_version,
                  minor: detail.live_minor_version,
                },
                {
                  major: detail.form_version,
                  minor: detail.form_minor_version,
                },
              ) > 0 ? (
              <span className="ml-1 text-accent">
                (live:{" "}
                {formatVersion(
                  detail.live_form_version,
                  detail.live_minor_version,
                )}
                )
              </span>
            ) : (
              <span className="ml-1 text-muted">(latest)</span>
            )}
          </span>
          <span className="font-mono text-xs text-muted">
            started {formatTimestamp(detail.created_at)}
          </span>
          {detail.terminated_at ? (
            <span className="font-mono text-xs text-muted">
              ended {formatTimestamp(detail.terminated_at)}
            </span>
          ) : null}
          {canRepin ? (
            <RepinControl
              formId={formId}
              submissionId={submissionId}
              fromVersion={detail.form_version}
              toVersion={detail.live_form_version}
              fromMinorVersion={detail.form_minor_version}
              toMinorVersion={detail.live_minor_version}
              onRepinned={() => refetch()}
            />
          ) : null}
        </div>
        {hasMultipleVersions ? (
          <VersionPicker
            options={detail.available_versions}
            selected={detail.viewing_version_id}
            onSelect={(id, isActive) =>
              setPickedVersionId(isActive ? undefined : id)
            }
          />
        ) : null}
        <a
          href={`/forms/${encodeURIComponent(formId)}/form/submission/${encodeURIComponent(
            detail.submission_id ?? detail.handle,
          )}`}
          className="mt-3 inline-block font-mono text-xs uppercase tracking-wider text-accent hover:text-accent-hover"
        >
          View the run →
        </a>
      </header>

      {detail.form_version_compile_error ? (
        <div className="border border-warning bg-surface-muted p-4">
          <p className="font-mono text-[11px] uppercase tracking-wider text-warning">
            Form source unavailable — view only
          </p>
          <p className="mt-1 text-sm text-ink">
            This submission's pinned form source no longer compiles
            cleanly. You can view all recorded data, history, and
            events, but advancing the submission, submitting new
            step values, and clearing are disabled until the source
            is repaired — or you re-pin the submission to a
            compatible version via the version picker above.
          </p>
          <p className="mt-2 font-mono text-[11px] text-muted break-all">
            {detail.form_version_compile_error}
          </p>
        </div>
      ) : null}

      {!detail.is_viewing_active ? (
        <div className="border border-muted bg-surface-muted p-4">
          <p className="font-mono text-[11px] uppercase tracking-wider text-muted">
            Read-only history
          </p>
          <p className="mt-1 text-sm text-ink">
            Viewing the frozen{" "}
            {formatVersion(
              detail.viewing_version,
              detail.viewing_minor_version,
            )}{" "}
            chain. This data cannot be edited or rerun; switch back to{" "}
            {formatVersion(detail.form_version, detail.form_minor_version)}{" "}
            (active) to interact with the submission.
          </p>
        </div>
      ) : null}

      {detail.error ? (
        <div className="border border-error bg-surface p-5">
          <p className="font-mono text-[11px] uppercase tracking-wider text-error">
            Failed
          </p>
          <p className="mt-1 text-sm text-ink">{detail.error}</p>
        </div>
      ) : null}

      <section>
        {/* Tab strip — Graph is default per Airflow conventions;
            List preserves the existing flat view. URL-backed so
            the choice survives a page share. */}
        <div className="mb-4 flex items-end justify-between gap-4 border-b border-border">
          <div className="flex gap-1">
            {(["graph", "list"] as const).map((t) => {
              const isActive = tab === t;
              return (
                <button
                  key={t}
                  type="button"
                  onClick={() => setTab(t)}
                  className={[
                    "font-mono text-xs uppercase tracking-[0.16em] px-3 py-2 border-b -mb-px",
                    isActive
                      ? "border-ink text-ink"
                      : "border-transparent text-muted hover:text-ink",
                  ].join(" ")}
                >
                  {t === "graph" ? "Graph" : "List"}
                </button>
              );
            })}
          </div>
          <span className="font-mono text-[11px] uppercase tracking-wider text-muted pb-2">
            Steps
          </span>
        </div>
        {tab === "graph" ? (
          // Fixed-height container — the graph canvas needs a
          // bounded box to lay out and pan within. 520px reads
          // comfortably in a wide page and is large enough for
          // most workflows; complex graphs pan/zoom.
          <div className="border border-border bg-surface" style={{ height: 520 }}>
            <SubmissionGraph
              formId={formId}
              steps={detail.steps}
              onNodeClick={handleGraphNodeClick}
            />
          </div>
        ) : detail.steps.length === 0 ? (
          <p className="text-muted text-sm">No steps recorded.</p>
        ) : (
          detail.steps.map((step) => (
            <div
              key={step.seq}
              ref={(el) => {
                if (el) stepBlockRefs.current.set(step.node_id, el);
                else stepBlockRefs.current.delete(step.node_id);
              }}
              // Flash effect when the user clicks a graph node —
              // accent ring fades after ~1.5s so the eye finds the
              // block without permanent highlight noise.
              className={
                flashStep === step.node_id
                  ? "transition-shadow duration-300 ring-2 ring-accent ring-offset-2 ring-offset-bg"
                  : "transition-shadow duration-300"
              }
            >
              <StepBlock step={step} />
            </div>
          ))
        )}
      </section>

      <Section title="History">
        <ol>
          {detail.events.map((event, i) => (
            <EventLine key={i} event={event} />
          ))}
        </ol>
      </Section>
    </div>
  );
}

export function VersionPicker({
  options,
  selected,
  onSelect,
}: {
  options: VersionOption[];
  selected: number;
  onSelect: (id: number, isActive: boolean) => void;
}) {
  return (
    <div
      role="tablist"
      aria-label="Submission version"
      className="mt-3 inline-flex items-center border border-muted"
    >
      {options.map((opt) => {
        const isSelected = opt.id === selected;
        return (
          <button
            key={opt.id}
            role="tab"
            aria-selected={isSelected}
            type="button"
            onClick={() => onSelect(opt.id, opt.is_active)}
            className={`px-3 py-1 font-mono text-xs uppercase tracking-wider transition-colors ${
              isSelected
                ? "bg-ink text-surface"
                : "bg-surface text-muted hover:text-ink"
            }`}
          >
            {formatVersion(opt.version, opt.minor_version)}
            {opt.is_active ? (
              <span className="ml-1 lowercase opacity-60">(active)</span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <section>
      <h3 className="mb-2 font-mono text-xs uppercase tracking-[0.15em] text-muted">
        {title}
      </h3>
      {children}
    </section>
  );
}

export function StepBlock({ step }: { step: StepDetailRow }) {
  const heading = step.title ?? step.node_id;
  const values = step.form_values
    ? Object.entries(step.form_values)
    : [];
  // Prefer chain_outputs (every backend's return, keyed by producer
  // name) over the legacy `backend_return` (just the first one).
  // For older submissions persisted before chain_outputs existed,
  // chain_outputs is null and we fall back to backend_return as a
  // single "returned" row.
  const chainEntries = step.chain_outputs
    ? Object.entries(step.chain_outputs)
    : [];
  const hasChain = chainEntries.length > 0;
  const hasLegacyReturn =
    !hasChain &&
    step.backend_return !== null &&
    step.backend_return !== undefined;

  return (
    <div className="border-t border-border py-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="font-mono text-[11px] uppercase tracking-wider text-muted">
            Step {String(step.seq + 1).padStart(2, "0")} · {step.kind}
          </p>
          <h4 className="mt-0.5 font-medium text-ink">{heading}</h4>
          {step.title && step.title !== step.node_id ? (
            <p className="font-mono text-xs text-muted">{step.node_id}</p>
          ) : null}
        </div>
        <StatePill state={step.state} />
      </div>

      {values.length > 0 ? (
        <dl className="mt-3 border-l border-border pl-3">
          {values.map(([key, value]) => (
            <div key={key} className="flex gap-3 py-0.5">
              <dt className="min-w-[7rem] shrink-0 font-mono text-xs text-muted">
                {key}
              </dt>
              <dd className="break-all font-mono text-xs text-ink">
                {renderValueWithLabels(step, key, value)}
              </dd>
            </div>
          ))}
        </dl>
      ) : null}

      {hasChain ? (
        <dl className="mt-3 border-l border-border pl-3">
          {chainEntries.map(([name, value]) => (
            <div key={name} className="flex gap-3 py-0.5">
              <dt className="min-w-[7rem] shrink-0 font-mono text-xs text-muted">
                {name}
              </dt>
              <dd className="break-all font-mono text-xs text-ink">
                {formatValue(value)}
              </dd>
            </div>
          ))}
        </dl>
      ) : hasLegacyReturn ? (
        <div className="mt-3 flex gap-3 border-l border-border pl-3">
          <span className="min-w-[7rem] shrink-0 font-mono text-xs text-muted">
            returned
          </span>
          <span className="break-all font-mono text-xs text-ink">
            {formatValue(step.backend_return)}
          </span>
        </div>
      ) : null}

      {step.submitted_at ? (
        <p className="mt-2 font-mono text-[11px] text-muted">
          submitted {formatTimestamp(step.submitted_at)}
        </p>
      ) : null}

      {step.assignments && step.assignments.length > 0 ? (
        <AssignmentsList assignments={step.assignments} />
      ) : null}
    </div>
  );
}

/** Inline list of child submissions spawned from this step by an
 *  Assign operator. Renders as chips with the child form title (link
 *  to the child's detail page), the granted role, the assignee, and
 *  the child's current state — or a "revoked" badge for revoked
 *  grants. Empty when no assignments fired. */
function AssignmentsList({ assignments }: { assignments: AssignedChild[] }) {
  return (
    <div className="mt-4 border border-border bg-surface px-3 py-3">
      <p className="mb-2 font-mono text-[10px] uppercase tracking-wider text-muted">
        Assigned ({assignments.length})
      </p>
      <ul className="space-y-1.5">
        {assignments.map((a) => {
          const subId = a.child_submission_id ?? a.child_submission_handle;
          const href = `/forms/${encodeURIComponent(a.child_form_id)}/submissions/${encodeURIComponent(subId)}`;
          const isRevoked = a.revoked_at !== null;
          return (
            <li
              key={a.assignment_id}
              className="flex items-baseline justify-between gap-3 text-xs"
            >
              <div className="min-w-0 flex-1">
                <Link
                  to={href}
                  className="font-medium text-ink hover:text-accent"
                >
                  {a.child_form_title}
                </Link>
                <span className="ml-2 font-mono text-[10px] uppercase tracking-wider text-muted">
                  {a.role_id}
                </span>
                {a.assignee_username ? (
                  <span className="ml-2 font-mono text-[11px] text-muted">
                    → {a.assignee_username}
                  </span>
                ) : null}
              </div>
              <div className="flex shrink-0 items-center gap-2 font-mono text-[10px] uppercase tracking-wider">
                {isRevoked ? (
                  <span className="text-muted">revoked</span>
                ) : (
                  <span
                    className={
                      a.child_submission_state === "success"
                        ? "text-muted"
                        : a.child_submission_state === "failed"
                          ? "text-error"
                          : "text-accent"
                    }
                  >
                    {a.child_submission_state}
                  </span>
                )}
                <span className="text-muted">
                  {formatTimestamp(a.granted_at)}
                </span>
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export function EventLine({ event }: { event: EventRow }) {
  return (
    <li className="flex gap-3 border-b border-border py-2">
      <span className="w-32 shrink-0 font-mono text-[11px] text-muted">
        {formatTimestamp(event.occurred_at)}
      </span>
      <span className="grow text-sm text-ink">{humanizeEvent(event.type)}</span>
      {event.node_id ? (
        <span className="shrink-0 font-mono text-xs text-muted">
          {event.node_id}
        </span>
      ) : null}
    </li>
  );
}

function humanizeEvent(type: string): string {
  const text = type.replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  if (value === "") return "(empty)";
  return String(value);
}

/**
 * Render a single form-value entry, resolving picker identifiers to
 * their display labels when the backend provided a label map.
 *
 * `step.value_labels` is keyed by field name; the inner map is
 * `{identifier_string: label}`. `step.value_kinds` tags the field
 * with the identifier kind (e.g. `frontflow_user_id`) — surfaced
 * here for future deep-linking (e.g. linking a user_id label to its
 * /users/:id page), kept readable now via the `(identifier)` suffix
 * so the underlying value stays visible.
 *
 * Falls through to plain `formatValue` for everything that isn't a
 * picker — most fields don't have a label map.
 */
function renderValueWithLabels(
  step: StepDetailRow,
  key: string,
  value: unknown,
): string {
  const labels = step.value_labels?.[key];
  // No label map for this field → plain formatting.
  if (!labels) return formatValue(value);
  // Surface the identifier kind in a debug tooltip later if needed;
  // referenced here so the symbol stays live in the bundle.
  const _kind = step.value_kinds?.[key];
  void _kind;
  if (Array.isArray(value)) {
    if (value.length === 0) return "—";
    return value
      .map((v) => {
        const id = String(v);
        const label = labels[id];
        return label ? `${label} (${id})` : id;
      })
      .join(", ");
  }
  if (value === null || value === undefined) return "—";
  const id = String(value);
  const label = labels[id];
  return label ? `${label} (${id})` : formatValue(value);
}

// --- Re-pin affordance ----------------------------------------------------

interface RepinControlProps {
  formId: string;
  submissionId: string;
  fromVersion: number;
  toVersion: number;
  /** Minor counterparts. Default 0 to preserve compatibility with
   *  call sites that haven't been threaded through yet. */
  fromMinorVersion?: number;
  toMinorVersion?: number;
  onRepinned: () => void;
}

export function RepinControl({
  formId,
  submissionId,
  fromVersion,
  toVersion,
  fromMinorVersion = 0,
  toMinorVersion = 0,
  onRepinned,
}: RepinControlProps) {
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [issues, setIssues] = useState<RepinIssue[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setOpen(false);
    setPending(false);
    setIssues(null);
    setError(null);
  };

  const handleConfirm = async (force: boolean = false) => {
    setPending(true);
    setError(null);
    if (!force) {
      // A fresh attempt — clear any prior issues. On a force retry
      // we keep the issues visible so the user can see what they
      // chose to override.
      setIssues(null);
    }
    try {
      const res = await repinSubmission(formId, submissionId, { force });
      if (res.repinned) {
        onRepinned();
        reset();
        return;
      }
      setIssues(res.issues);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setPending(false);
    }
  };

  const toLabel = formatVersion(toVersion, toMinorVersion);
  const fromLabel = formatVersion(fromVersion, fromMinorVersion);

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="font-mono text-[10px] uppercase tracking-[0.18em] text-accent hover:text-accent-hover underline underline-offset-2"
      >
        Re-pin to {toLabel}
      </button>
      {open ? (
        <RepinDialog
          fromLabel={fromLabel}
          toLabel={toLabel}
          pending={pending}
          issues={issues}
          error={error}
          onConfirm={handleConfirm}
          onClose={reset}
        />
      ) : null}
    </>
  );
}

function RepinDialog({
  fromLabel,
  toLabel,
  pending,
  issues,
  error,
  onConfirm,
  onClose,
}: {
  fromLabel: string;
  toLabel: string;
  pending: boolean;
  issues: RepinIssue[] | null;
  error: string | null;
  onConfirm: (force?: boolean) => void;
  onClose: () => void;
}) {
  // When the standard repin returns shape-incompatibility issues,
  // we surface a force-repin escape hatch. It's gated behind an
  // explicit acknowledgement checkbox because force-repin's
  // semantics are aggressive: the current active chain is frozen
  // into read-only history, the submission's `state` resets to
  // in-flight on the new version, and any in-progress data is
  // preserved but no longer editable through the normal flow.
  // Past versions remain viewable via the version picker.
  const [forceAck, setForceAck] = useState(false);
  const hasIssues = issues !== null && issues.length > 0;
  return (
    <Modal open={true} onClose={pending ? () => {} : onClose} preventDismiss={pending}>
      <div className="flex flex-col gap-4">
        <h2 className="font-display text-base uppercase tracking-[0.18em] text-ink">
          Re-pin to {toLabel}
        </h2>
        <p className="text-sm text-muted">
          This submission was started on version <strong>{fromLabel}</strong>{" "}
          of the form. Re-pinning moves it to the current live version{" "}
          <strong>{toLabel}</strong>, so subsequent steps run on the new
          code. Already-submitted steps stay as recorded — but their shape
          must still match the new form, or the re-pin is refused.
        </p>

        {hasIssues ? (
          <div className="border border-error bg-surface/40 p-3">
            <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-error">
              Cannot re-pin cleanly — {issues!.length}{" "}
              {issues!.length === 1 ? "issue" : "issues"}
            </p>
            <ul className="mt-2 space-y-1.5">
              {issues!.map((it, i) => (
                <li key={i} className="text-xs text-ink">
                  <span className="font-mono text-muted">{it.kind}</span>{" "}
                  — {it.detail}
                </li>
              ))}
            </ul>
            <p className="mt-3 text-xs text-muted">
              The recommended fix is to make the form's shape backward-
              compatible with already-submitted data, then re-pin
              normally.
            </p>

            {/*
              Force-repin escape hatch. Gated behind an explicit
              acknowledgement because the semantics matter:
              `validate_repin` is run against the submission's chain,
              the longest VALID PREFIX is kept on the active chain
              (no data loss for those steps), and everything from the
              first invalidated step onward is moved to read-only
              history under the prior form_version_id. The submission
              resumes in-flight at the first dropped step.
            */}
            <div className="mt-4 border-t border-error/40 pt-3">
              <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-ink">
                Or: force the re-pin
              </p>
              <p className="mt-1 text-xs text-muted">
                Forcing keeps the steps that ARE still compatible and
                drops only the ones flagged above. The dropped steps
                become read-only history (viewable via the version
                picker); steps before them stay on the active chain
                with their data intact. The submission resumes
                in-flight at the first dropped step on {toLabel}, so
                you re-complete from there forward. Any side effects
                from the dropped steps (S3 uploads, Airflow runs,
                etc.) are NOT rewound.
              </p>
              <label className="mt-3 flex items-start gap-2 text-xs text-ink cursor-pointer">
                <input
                  type="checkbox"
                  checked={forceAck}
                  onChange={(e) => setForceAck(e.target.checked)}
                  disabled={pending}
                  className="mt-0.5"
                />
                <span>
                  I understand the flagged step(s) and everything after
                  them will be moved to history; the submission resumes
                  in-flight on {toLabel}.
                </span>
              </label>
            </div>
          </div>
        ) : null}

        {error ? <p className="text-xs text-error">{error}</p> : null}

        <div className="flex items-center justify-end gap-3">
          <button
            type="button"
            onClick={onClose}
            disabled={pending}
            className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted hover:text-ink disabled:opacity-50"
          >
            {hasIssues ? "Close" : "Cancel"}
          </button>
          {!hasIssues ? (
            <button
              type="button"
              onClick={() => onConfirm(false)}
              disabled={pending}
              className="border border-ink bg-ink px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.18em] text-bg hover:bg-accent-hover disabled:opacity-50"
            >
              {pending ? "Re-pinning…" : "Re-pin"}
            </button>
          ) : (
            <button
              type="button"
              onClick={() => onConfirm(true)}
              disabled={pending || !forceAck}
              // Distinct danger styling so it doesn't look like the
              // normal repin button — error border + label calls
              // out what this is.
              className="border border-error bg-error px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.18em] text-bg hover:opacity-90 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {pending ? "Forcing…" : "Force re-pin"}
            </button>
          )}
        </div>
      </div>
    </Modal>
  );
}

// Re-export so consumers don't import unused types.
export type { SubmissionDetail };
