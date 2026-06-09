import { useState } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import { useQueryClient, useMutation } from "@tanstack/react-query";
import { useAuth } from "../auth/AuthContext";
import { useUserAssignments } from "../hooks/useUserAssignments";
import {
  ApiError,
  revokeAssignment,
  revokeAllForUserOnSubmission,
  type UserAssignment,
} from "../lib/api";
import { formatTimestamp } from "../lib/format";

/**
 * Admin Access page at /users/:userId — every SubmissionAssignment
 * ever granted to or revoked from this user, with a Revoke button
 * per active row. Drives the bundle test anchor `/assignments/`
 * because the Revoke handler calls `revokeAssignment(id)`, which
 * encodes the URL `/assignments/{id}/revoke`.
 *
 * Admin-only on the server; mirrored here as a redirect so non-admin
 * users don't render an empty table.
 */
export default function UserDetailPage() {
  const { user } = useAuth();
  const { userId: rawId } = useParams();
  const [includeRevoked, setIncludeRevoked] = useState(true);

  if (user === undefined) {
    return (
      <main className="relative z-10 mx-auto max-w-7xl px-6 pt-24 pb-16">
        <p className="font-display text-2xl text-ink opacity-30">Loading…</p>
      </main>
    );
  }
  if (user === null) return <Navigate to="/login" replace />;
  if (!user.is_admin) return <Navigate to="/forms" replace />;

  const userId = rawId ? Number(rawId) : undefined;
  return (
    <main className="relative z-10 mx-auto max-w-7xl px-6 pt-24 pb-16">
      <header className="mb-10">
        <Link
          to="/users"
          className="font-mono text-xs uppercase tracking-wider text-muted hover:text-accent"
        >
          ← Users
        </Link>
        <h1 className="mt-3 font-display text-5xl font-bold leading-[1.0] text-ink">
          User #{rawId}
        </h1>
        <p className="mt-3 max-w-xl text-sm text-muted">
          Every assignment ever granted to or revoked from this user.
          Active rows can be revoked here; revoked rows stay for audit.
        </p>
      </header>

      {userId === undefined || Number.isNaN(userId) ? (
        <p className="text-error text-sm">Invalid user id in URL.</p>
      ) : (
        <AccessTable
          userId={userId}
          includeRevoked={includeRevoked}
          onToggleIncludeRevoked={() => setIncludeRevoked((v) => !v)}
        />
      )}
    </main>
  );
}

function AccessTable({
  userId,
  includeRevoked,
  onToggleIncludeRevoked,
}: {
  userId: number;
  includeRevoked: boolean;
  onToggleIncludeRevoked: () => void;
}) {
  const { data, error, isLoading } = useUserAssignments(userId, includeRevoked);

  return (
    <>
      <div className="mb-4 flex items-center justify-between gap-4">
        <p className="font-mono text-xs uppercase tracking-wider text-muted">
          {data ? `${data.length} ${data.length === 1 ? "row" : "rows"}` : ""}
        </p>
        <label className="flex cursor-pointer items-center gap-2 font-mono text-xs uppercase tracking-wider text-muted">
          <input
            type="checkbox"
            checked={includeRevoked}
            onChange={onToggleIncludeRevoked}
            className="h-3 w-3"
          />
          Include revoked
        </label>
      </div>

      {isLoading ? (
        <p className="font-display text-2xl text-ink opacity-30">Loading…</p>
      ) : error ? (
        <div className="border border-error bg-surface p-6">
          <p className="text-error text-sm">
            {error instanceof ApiError
              ? `Couldn't load assignments (${error.status}): ${error.message}`
              : `Couldn't load assignments: ${(error as Error).message}`}
          </p>
        </div>
      ) : !data || data.length === 0 ? (
        <EmptyState />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr className="border-b border-border">
                <Th>Form</Th>
                <Th>Role</Th>
                <Th>State</Th>
                <Th>Granted</Th>
                <Th>By</Th>
                <Th>Status</Th>
                <Th>Actions</Th>
              </tr>
            </thead>
            <tbody>
              {groupBySubmission(data).map(({ handle, rows }) => (
                <SubmissionGroup
                  key={handle}
                  handle={handle}
                  rows={rows}
                  userId={userId}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function Row({ row, userId }: { row: UserAssignment; userId: number }) {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const mutation = useMutation({
    mutationFn: () => revokeAssignment(row.assignment_id),
    onSuccess: () => {
      // Invalidate every variant of this user's assignments query
      // (with and without include_revoked) so toggling the filter
      // also shows the freshly-revoked row.
      queryClient.invalidateQueries({ queryKey: ["userAssignments", userId] });
    },
    onError: (err) => {
      setError(
        err instanceof ApiError
          ? `Couldn't revoke (${err.status}): ${err.message}`
          : `Couldn't revoke: ${(err as Error).message}`,
      );
    },
  });
  const subId = row.submission_id ?? row.submission_handle;
  const href = `/forms/${encodeURIComponent(row.form_id)}/form/submission/${encodeURIComponent(subId)}`;
  const isRevoked = row.revoked_at !== null;
  return (
    <tr className="border-b border-border align-top">
      <td className="py-4 pr-6">
        <Link to={href} className="font-medium text-ink hover:text-accent">
          {row.form_title}
        </Link>
        <div className="mt-1 font-mono text-xs text-muted">{row.form_id}</div>
      </td>
      <td className="py-4 pr-6 font-mono text-xs uppercase tracking-wider text-ink">
        {row.role_id}
      </td>
      <td className="py-4 pr-6">
        <StatePill state={row.submission_state} />
      </td>
      <td className="py-4 pr-6 font-mono text-xs text-muted">
        {formatTimestamp(row.granted_at)}
      </td>
      <td className="py-4 pr-6 font-mono text-xs text-muted">
        {row.granted_by_username ?? `#${row.granted_by_user_id}`}
      </td>
      <td className="py-4 pr-6 font-mono text-xs">
        {isRevoked ? (
          <span className="text-muted">
            revoked
            {row.revoked_by_username ? ` by ${row.revoked_by_username}` : ""}{" "}
            {row.revoked_at ? formatTimestamp(row.revoked_at) : ""}
          </span>
        ) : (
          <span className="text-accent">active</span>
        )}
      </td>
      <td className="py-4 pr-6">
        {isRevoked ? (
          <span className="font-mono text-xs text-muted">—</span>
        ) : (
          <>
            <button
              type="button"
              disabled={mutation.isPending}
              onClick={() => {
                if (
                  window.confirm(
                    `Revoke ${row.role_id} access on ${row.form_title}?`,
                  )
                )
                  mutation.mutate();
              }}
              className="font-mono text-xs uppercase tracking-wider text-error hover:underline disabled:opacity-50"
            >
              {mutation.isPending ? "Revoking…" : "Revoke"}
            </button>
            {error ? (
              <div className="mt-1 font-mono text-xs text-error">{error}</div>
            ) : null}
          </>
        )}
      </td>
    </tr>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return (
    <th className="py-2 pr-6 text-left font-mono text-[11px] font-medium uppercase tracking-wider text-muted">
      {children}
    </th>
  );
}

/** Group assignment rows by submission_handle, preserving the
 *  newest-first order of the first row in each group. Used so the
 *  table can render a per-submission group header with a bulk-revoke
 *  button when the user holds multiple active grants on the same
 *  submission. */
function groupBySubmission(
  rows: UserAssignment[],
): Array<{ handle: string; rows: UserAssignment[] }> {
  const index = new Map<string, UserAssignment[]>();
  const order: string[] = [];
  for (const r of rows) {
    const h = r.submission_handle;
    let bucket = index.get(h);
    if (!bucket) {
      bucket = [];
      index.set(h, bucket);
      order.push(h);
    }
    bucket.push(r);
  }
  return order.map((h) => ({ handle: h, rows: index.get(h)! }));
}

/** All assignment rows for one submission, rendered consecutively
 *  in the table with a thin "group header" row above them. The
 *  header carries the bulk-revoke button when there are 2+ active
 *  rows in the group — for a single-row group, the per-row Revoke
 *  button is enough; surfacing a second button there would be
 *  redundant and visually loud. */
function SubmissionGroup({
  handle,
  rows,
  userId,
}: {
  handle: string;
  rows: UserAssignment[];
  userId: number;
}) {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const activeCount = rows.filter((r) => r.revoked_at === null).length;
  const showBulk = activeCount >= 2;
  // Form title/id for the bulk-revoke confirm dialog — same form
  // across all rows in the group (one submission belongs to one
  // form), so any row works.
  const firstRow = rows[0];
  const mutation = useMutation({
    mutationFn: () => revokeAllForUserOnSubmission(handle, userId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["userAssignments", userId] });
    },
    onError: (err) => {
      setError(
        err instanceof ApiError
          ? `Couldn't revoke all (${err.status}): ${err.message}`
          : `Couldn't revoke all: ${(err as Error).message}`,
      );
    },
  });
  return (
    <>
      {showBulk ? (
        <tr className="bg-surface/50">
          <td colSpan={7} className="px-0 py-2">
            <div className="flex items-baseline justify-between gap-4 px-0">
              <span className="font-mono text-[10px] uppercase tracking-wider text-muted">
                {activeCount} active grants on{" "}
                <span className="text-ink">{firstRow.form_title}</span>{" "}
                <span className="text-muted">({handle})</span>
              </span>
              <div className="flex items-center gap-3">
                {error ? (
                  <span className="font-mono text-xs text-error">{error}</span>
                ) : null}
                <button
                  type="button"
                  disabled={mutation.isPending}
                  onClick={() => {
                    if (
                      window.confirm(
                        `Revoke ALL ${activeCount} active grants for this user on ${firstRow.form_title}?`,
                      )
                    )
                      mutation.mutate();
                  }}
                  className="font-mono text-[11px] uppercase tracking-wider text-error hover:underline disabled:opacity-50"
                >
                  {mutation.isPending
                    ? "Revoking all…"
                    : "Revoke all on submission"}
                </button>
              </div>
            </div>
          </td>
        </tr>
      ) : null}
      {rows.map((row) => (
        <Row key={row.assignment_id} row={row} userId={userId} />
      ))}
    </>
  );
}

function StatePill({ state }: { state: string }) {
  const cls =
    state === "running"
      ? "border-accent text-accent"
      : state === "success"
        ? "border-border text-muted"
        : state === "failed"
          ? "border-error text-error"
          : "border-border text-muted";
  return (
    <span
      className={`border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider ${cls}`}
    >
      {state}
    </span>
  );
}

function EmptyState() {
  return (
    <div className="border border-border bg-surface px-8 py-12 text-center">
      <p className="font-display text-2xl text-ink opacity-50">
        No assignments for this user.
      </p>
      <p className="mt-3 font-mono text-xs uppercase tracking-wider text-muted">
        Assignments granted to or revoked from this user will appear
        here as soon as one is created.
      </p>
    </div>
  );
}
