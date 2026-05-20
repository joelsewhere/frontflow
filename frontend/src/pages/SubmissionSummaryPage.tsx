import { useState, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ApiError,
  type EventRow,
  type RepinIssue,
  type StepDetailRow,
  repinSubmission,
} from "../lib/api";
import { formatTimestamp } from "../lib/format";
import { useSubmissionDetail } from "../hooks/useSubmissionDetail";
import { useAuth } from "../auth/AuthContext";
import { StatePill } from "../components/listing/StatePill";
import { Modal } from "../components/ui/Modal";

/**
 * Submission summary (`/forms/:formId/submissions/:submissionId`) — the
 * persisted record of one submission: every step with the data it
 * captured, and the append-only event history. A link leads into the
 * live run view. Distinct from the run itself, which is under `/form`.
 */
export default function SubmissionSummaryPage() {
  const { formId, submissionId } = useParams<{
    formId: string;
    submissionId: string;
  }>();
  const { data: detail, error, isLoading, refetch } = useSubmissionDetail(
    formId,
    submissionId,
  );
  const { user } = useAuth();

  const canRepin =
    !!user?.is_admin &&
    !!detail &&
    detail.live_form_version > detail.form_version;

  return (
    <main className="relative z-10 mx-auto max-w-7xl px-6 pt-24 pb-16">
      <header className="mb-10">
        <Link
          to={`/forms/${encodeURIComponent(formId ?? "")}/submissions`}
          className="font-mono text-xs uppercase tracking-wider text-muted hover:text-accent"
        >
          ← Submissions
        </Link>
        <p className="mt-4 font-mono text-[11px] uppercase tracking-[0.25em] text-muted">
          Submission
        </p>
        <h1 className="mt-1 break-all font-mono text-3xl font-semibold text-ink">
          {detail?.submission_id ?? submissionId}
        </h1>
        {detail ? (
          <div className="mt-4 flex flex-wrap items-center gap-4">
            <StatePill state={detail.state} />
            <span className="font-mono text-xs text-muted">
              version {detail.form_version}
              {detail.live_form_version > detail.form_version ? (
                <span className="ml-1 text-accent">
                  (live: v{detail.live_form_version})
                </span>
              ) : null}
            </span>
            <span className="font-mono text-xs text-muted">
              started {formatTimestamp(detail.created_at)}
            </span>
            {detail.terminated_at ? (
              <span className="font-mono text-xs text-muted">
                ended {formatTimestamp(detail.terminated_at)}
              </span>
            ) : null}
            {canRepin && formId && submissionId ? (
              <RepinControl
                formId={formId}
                submissionId={submissionId}
                fromVersion={detail.form_version}
                toVersion={detail.live_form_version}
                onRepinned={() => refetch()}
              />
            ) : null}
          </div>
        ) : null}
        {detail && formId ? (
          <Link
            to={`/forms/${encodeURIComponent(formId)}/form/submission/${encodeURIComponent(
              detail.submission_id ?? detail.handle,
            )}`}
            className="mt-4 inline-block font-mono text-xs uppercase tracking-wider text-accent hover:text-accent-hover"
          >
            View the run →
          </Link>
        ) : null}
      </header>

      {isLoading ? (
        <p className="font-display text-2xl text-ink opacity-30">Loading…</p>
      ) : error ? (
        <div className="border border-error bg-surface p-6">
          <p className="text-error text-sm">
            {error instanceof ApiError && error.status === 404
              ? "This submission doesn't exist."
              : `Couldn't load this submission: ${
                  error instanceof ApiError ? error.message : "unknown error"
                }`}
          </p>
        </div>
      ) : detail ? (
        <>
          {detail.error ? (
            <div className="mb-10 border border-error bg-surface p-5">
              <p className="font-mono text-[11px] uppercase tracking-wider text-error">
                Failed
              </p>
              <p className="mt-1 text-sm text-ink">{detail.error}</p>
            </div>
          ) : null}

          <Section title="Steps">
            {detail.steps.length === 0 ? (
              <p className="text-muted text-sm">No steps recorded.</p>
            ) : (
              detail.steps.map((step) => (
                <StepBlock key={step.seq} step={step} />
              ))
            )}
          </Section>

          <Section title="History">
            <ol>
              {detail.events.map((event, i) => (
                <EventLine key={i} event={event} />
              ))}
            </ol>
          </Section>
        </>
      ) : null}
    </main>
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
    <section className="mt-12 first:mt-0">
      <h2 className="mb-2 font-mono text-xs uppercase tracking-[0.15em] text-muted">
        {title}
      </h2>
      {children}
    </section>
  );
}

function StepBlock({ step }: { step: StepDetailRow }) {
  const heading = step.title ?? step.node_id;
  const values = step.form_values
    ? Object.entries(step.form_values)
    : [];
  const hasReturn =
    step.backend_return !== null && step.backend_return !== undefined;

  return (
    <div className="border-t border-border py-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="font-mono text-[11px] uppercase tracking-wider text-muted">
            Step {String(step.seq + 1).padStart(2, "0")} · {step.kind}
          </p>
          <h3 className="mt-0.5 font-medium text-ink">{heading}</h3>
          {step.title && step.title !== step.node_id ? (
            <p className="font-mono text-xs text-muted">{step.node_id}</p>
          ) : null}
        </div>
        <StatePill state={step.state} />
      </div>

      {values.length > 0 ? (
        <dl className="mt-3 border-l border-border pl-4">
          {values.map(([key, value]) => (
            <div key={key} className="flex gap-4 py-0.5">
              <dt className="min-w-[9rem] shrink-0 font-mono text-xs text-muted">
                {key}
              </dt>
              <dd className="break-all font-mono text-xs text-ink">
                {formatValue(value)}
              </dd>
            </div>
          ))}
        </dl>
      ) : null}

      {hasReturn ? (
        <div className="mt-3 flex gap-4 border-l border-border pl-4">
          <span className="min-w-[9rem] shrink-0 font-mono text-xs text-muted">
            returned
          </span>
          <span className="break-all font-mono text-xs text-ink">
            {formatValue(step.backend_return)}
          </span>
        </div>
      ) : null}

      {step.submitted_at ? (
        <p className="mt-3 font-mono text-[11px] text-muted">
          submitted {formatTimestamp(step.submitted_at)}
        </p>
      ) : null}
    </div>
  );
}

function EventLine({ event }: { event: EventRow }) {
  return (
    <li className="flex gap-4 border-b border-border py-2">
      <span className="w-44 shrink-0 font-mono text-[11px] text-muted">
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

/** Turn an event type token into a sentence — `step_submitted` →
 *  "Step submitted". */
function humanizeEvent(type: string): string {
  const text = type.replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Render an arbitrary captured value for display. */
function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  if (value === "") return "(empty)";
  return String(value);
}

/** Admin-only affordance to re-pin a submission to the form's current
 *  live version. Opens a confirmation modal; on a clean re-pin it
 *  refetches the submission detail. On a 409 mismatch the modal lists
 *  the issues blocking the re-pin so the author can fix the form. */
interface RepinControlProps {
  formId: string;
  submissionId: string;
  fromVersion: number;
  toVersion: number;
  onRepinned: () => void;
}

function RepinControl({
  formId,
  submissionId,
  fromVersion,
  toVersion,
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

  const handleConfirm = async () => {
    setPending(true);
    setError(null);
    setIssues(null);
    try {
      const res = await repinSubmission(formId, submissionId);
      if (res.repinned) {
        onRepinned();
        reset();
        return;
      }
      // 409 came back as a normal response with issues — show them.
      setIssues(res.issues);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setPending(false);
    }
  };

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="font-mono text-[10px] uppercase tracking-[0.18em] text-accent hover:text-accent-hover underline underline-offset-2"
      >
        Re-pin to v{toVersion}
      </button>
      {open ? (
        <RepinDialog
          fromVersion={fromVersion}
          toVersion={toVersion}
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

interface RepinDialogProps {
  fromVersion: number;
  toVersion: number;
  pending: boolean;
  issues: RepinIssue[] | null;
  error: string | null;
  onConfirm: () => void;
  onClose: () => void;
}

function RepinDialog({
  fromVersion,
  toVersion,
  pending,
  issues,
  error,
  onConfirm,
  onClose,
}: RepinDialogProps) {
  return (
    <Modal open={true} onClose={pending ? () => {} : onClose} preventDismiss={pending}>
      <div className="flex flex-col gap-4">
        <h2 className="font-display text-base uppercase tracking-[0.18em] text-ink">
          Re-pin to v{toVersion}
        </h2>
        <p className="text-sm text-muted">
          This submission was started on version <strong>v{fromVersion}</strong>{" "}
          of the form. Re-pinning moves it to the current live version{" "}
          <strong>v{toVersion}</strong>, so subsequent steps run on the new
          code. Already-submitted steps stay as recorded — but their shape
          must still match the new form, or the re-pin is refused.
        </p>

        {issues && issues.length > 0 ? (
          <div className="border border-error bg-surface/40 p-3">
            <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-error">
              Cannot re-pin — {issues.length}{" "}
              {issues.length === 1 ? "issue" : "issues"}
            </p>
            <ul className="mt-2 space-y-1.5">
              {issues.map((it, i) => (
                <li key={i} className="text-xs text-ink">
                  <span className="font-mono text-muted">{it.kind}</span>{" "}
                  — {it.detail}
                </li>
              ))}
            </ul>
            <p className="mt-3 text-xs text-muted">
              Fix the form so already-submitted data still fits, then try
              again.
            </p>
          </div>
        ) : null}

        {error ? (
          <p className="text-xs text-error">{error}</p>
        ) : null}

        <div className="flex items-center justify-end gap-3">
          <button
            type="button"
            onClick={onClose}
            disabled={pending}
            className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted hover:text-ink disabled:opacity-50"
          >
            {issues && issues.length > 0 ? "Close" : "Cancel"}
          </button>
          {!issues || issues.length === 0 ? (
            <button
              type="button"
              onClick={onConfirm}
              disabled={pending}
              className="border border-ink bg-ink px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.18em] text-bg hover:bg-accent-hover disabled:opacity-50"
            >
              {pending ? "Re-pinning…" : "Re-pin"}
            </button>
          ) : null}
        </div>
      </div>
    </Modal>
  );
}
