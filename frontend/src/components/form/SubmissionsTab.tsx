import { useMemo, useState, type ReactNode } from "react";
import { ApiError, type SubmissionSummary } from "../../lib/api";
import { formatTimestamp } from "../../lib/format";
import { useFormSubmissions } from "../../hooks/useFormSubmissions";
import { StatePill } from "../listing/StatePill";

const PAGE_SIZE = 25;

interface SubmissionsTabProps {
  formId: string;
  onOpenSubmission: (id: string) => void;
}

/**
 * The form's full submission list — paginated client-side at 25 per
 * page (the hook fetches all rows; server-side pagination is a
 * roadmapped follow-up). Clicking a row opens the SubmissionDrawer
 * over the parent page.
 */
export function SubmissionsTab({
  formId,
  onOpenSubmission,
}: SubmissionsTabProps) {
  const { data: submissions, error, isLoading } = useFormSubmissions(formId);
  const [page, setPage] = useState(0); // zero-indexed

  const total = submissions?.length ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  // If `total` shrinks (e.g. a refetch loses a row), clamp the page so
  // we never render an empty out-of-range view.
  const safePage = Math.min(page, pageCount - 1);
  const pageRows = useMemo(() => {
    if (!submissions) return [];
    const start = safePage * PAGE_SIZE;
    return submissions.slice(start, start + PAGE_SIZE);
  }, [submissions, safePage]);

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
            ? "This form doesn't exist."
            : `Couldn't load submissions: ${
                error instanceof ApiError ? error.message : "unknown error"
              }`}
        </p>
      </div>
    );
  }
  if (!submissions || submissions.length === 0) {
    return <p className="text-muted text-sm">No submissions yet for this form.</p>;
  }

  const rangeStart = safePage * PAGE_SIZE + 1;
  const rangeEnd = Math.min(rangeStart + PAGE_SIZE - 1, total);

  return (
    <div>
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
            {pageRows.map((s) => (
              <SubmissionRow
                key={s.handle}
                s={s}
                onOpen={onOpenSubmission}
              />
            ))}
          </tbody>
        </table>
      </div>
      {pageCount > 1 ? (
        <Paginator
          page={safePage}
          pageCount={pageCount}
          rangeStart={rangeStart}
          rangeEnd={rangeEnd}
          total={total}
          onChange={setPage}
        />
      ) : null}
    </div>
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
  s,
  onOpen,
}: {
  s: SubmissionSummary;
  onOpen: (id: string) => void;
}) {
  const id = s.submission_id ?? s.handle;
  return (
    <tr
      onClick={() => onOpen(id)}
      className="cursor-pointer border-b border-border transition-colors hover:bg-surface"
    >
      <td className="py-4 pr-6">
        <span className="font-mono text-sm text-ink">{id}</span>
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

function Paginator({
  page,
  pageCount,
  rangeStart,
  rangeEnd,
  total,
  onChange,
}: {
  page: number;
  pageCount: number;
  rangeStart: number;
  rangeEnd: number;
  total: number;
  onChange: (page: number) => void;
}) {
  const canPrev = page > 0;
  const canNext = page < pageCount - 1;
  return (
    <div className="mt-4 flex items-center justify-between gap-4">
      <span className="font-mono text-xs text-muted">
        Showing {rangeStart}–{rangeEnd} of {total}
      </span>
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={() => canPrev && onChange(page - 1)}
          disabled={!canPrev}
          className="font-mono text-xs uppercase tracking-wider text-muted hover:text-ink disabled:opacity-30 disabled:hover:text-muted"
        >
          ← Prev
        </button>
        <span className="font-mono text-xs text-muted tabular-nums">
          {page + 1} / {pageCount}
        </span>
        <button
          type="button"
          onClick={() => canNext && onChange(page + 1)}
          disabled={!canNext}
          className="font-mono text-xs uppercase tracking-wider text-muted hover:text-ink disabled:opacity-30 disabled:hover:text-muted"
        >
          Next →
        </button>
      </div>
    </div>
  );
}
