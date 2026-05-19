import { type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, type SubmissionSummary } from "../lib/api";
import { formatTimestamp } from "../lib/format";
import { useFormDetail } from "../hooks/useFormDetail";
import { useFormSubmissions } from "../hooks/useFormSubmissions";
import { StatePill } from "../components/listing/StatePill";

/**
 * A form's submission list (`/forms/:formId/submissions`) — every
 * submission, newest first, with its state, version, and current step.
 * A submission links into the existing submission view, which renders
 * it against the form version it actually ran on.
 */
export default function FormSubmissionsPage() {
  const { formId } = useParams<{ formId: string }>();
  // The submissions endpoint doesn't carry the form's display name;
  // useFormDetail supplies it. It 404s for an archived form, so a
  // successful fetch also tells us the form is still live.
  const { data: form } = useFormDetail(formId);
  const { data: submissions, error, isLoading } = useFormSubmissions(formId);

  const heading = form?.title ?? formId ?? "Form";
  const isLive = Boolean(form);

  return (
    <main className="relative z-10 mx-auto max-w-7xl px-6 pt-24 pb-16">
      <header className="mb-12">
        <Link
          to="/forms"
          className="font-mono text-xs uppercase tracking-wider text-muted hover:text-accent"
        >
          ← All forms
        </Link>
        <h1 className="mt-4 font-display text-5xl font-bold leading-[1.0] text-ink">
          {heading}
        </h1>
        <div className="mt-4 flex items-center gap-4">
          {submissions ? (
            <span className="font-mono text-xs uppercase tracking-wider text-muted">
              {submissions.length}{" "}
              {submissions.length === 1 ? "submission" : "submissions"}
            </span>
          ) : null}
          {isLive && formId ? (
            <Link
              to={`/forms/${encodeURIComponent(formId)}/form`}
              className="font-mono text-xs uppercase tracking-wider text-accent hover:text-accent-hover"
            >
              Start a submission →
            </Link>
          ) : null}
        </div>
      </header>

      {isLoading ? (
        <p className="font-display text-2xl text-ink opacity-30">Loading…</p>
      ) : error ? (
        <div className="border border-error bg-surface p-6">
          <p className="text-error text-sm">
            {error instanceof ApiError && error.status === 404
              ? "This form doesn't exist."
              : `Couldn't load submissions: ${
                  error instanceof ApiError ? error.message : "unknown error"
                }`}
          </p>
        </div>
      ) : submissions && submissions.length === 0 ? (
        <p className="text-muted text-sm">No submissions yet for this form.</p>
      ) : submissions ? (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr className="border-b border-border">
                <Th>Submission</Th>
                <Th>State</Th>
                <Th>Version</Th>
                <Th>Started</Th>
                <Th>Step</Th>
              </tr>
            </thead>
            <tbody>
              {submissions.map((s) => (
                <SubmissionRow key={s.handle} formId={formId!} s={s} />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </main>
  );
}

function Th({ children }: { children: ReactNode }) {
  return (
    <th className="py-2 pr-6 text-left font-mono text-[11px] font-medium uppercase tracking-wider text-muted">
      {children}
    </th>
  );
}

function SubmissionRow({
  formId,
  s,
}: {
  formId: string;
  s: SubmissionSummary;
}) {
  const navigate = useNavigate();
  // Persisted submissions always have a minted id; fall back to the
  // handle defensively so a row is never unaddressable.
  const id = s.submission_id ?? s.handle;
  const to = `/forms/${encodeURIComponent(formId)}/submissions/${encodeURIComponent(id)}`;

  return (
    <tr
      onClick={() => navigate(to)}
      className="cursor-pointer border-b border-border transition-colors hover:bg-surface"
    >
      <td className="py-4 pr-6">
        <Link
          to={to}
          onClick={(e) => e.stopPropagation()}
          className="font-mono text-sm text-ink hover:text-accent"
        >
          {id}
        </Link>
      </td>
      <td className="py-4 pr-6">
        <StatePill state={s.state} />
      </td>
      <td className="py-4 pr-6 font-mono text-sm tabular-nums text-muted">
        v{s.form_version}
      </td>
      <td className="py-4 pr-6 font-mono text-xs text-muted">
        {formatTimestamp(s.created_at)}
      </td>
      <td className="py-4 font-mono text-xs text-muted">
        {s.current_step ?? "—"}
      </td>
    </tr>
  );
}
