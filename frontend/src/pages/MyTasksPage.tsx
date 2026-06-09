import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { useMyTasks } from "../hooks/useMyTasks";
import { ApiError, type MyTask } from "../lib/api";
import { formatTimestamp } from "../lib/format";

/**
 * /my-tasks — the signed-in user's inbox of every SubmissionAssignment
 * granted to them, newest first. Clicking a row opens the child
 * submission in form-fill mode (the resumable URL).
 *
 * Filter and search are intentionally V2 — this is the floor, the
 * helpful empty state explains where rows come from.
 */
export default function MyTasksPage() {
  const { user } = useAuth();
  const { data, error, isLoading } = useMyTasks();
  return (
    <main className="relative z-10 mx-auto max-w-7xl px-6 pt-24 pb-16">
      <header className="mb-12 flex items-end justify-between gap-6">
        <div>
          <h1 className="font-display text-5xl font-bold leading-[1.0] text-ink">
            My tasks
          </h1>
          {data ? (
            <p className="mt-4 font-mono text-xs uppercase tracking-wider text-muted">
              {data.length}{" "}
              {data.length === 1 ? "assignment" : "assignments"}
            </p>
          ) : null}
        </div>
        <div className="flex shrink-0 items-center gap-6">
          <Link
            to="/forms"
            className="font-mono text-xs uppercase tracking-wider text-muted hover:text-accent"
          >
            Forms →
          </Link>
          {user?.is_admin ? (
            <Link
              to="/users"
              className="font-mono text-xs uppercase tracking-wider text-muted hover:text-accent"
            >
              Users →
            </Link>
          ) : null}
        </div>
      </header>

      {isLoading ? (
        <p className="font-display text-2xl text-ink opacity-30">Loading…</p>
      ) : error ? (
        <div className="border border-error bg-surface p-6">
          <p className="text-error text-sm">
            {error instanceof ApiError
              ? `Couldn't load tasks (${error.status}): ${error.message}`
              : `Couldn't load tasks: ${(error as Error).message}`}
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
              </tr>
            </thead>
            <tbody>
              {data.map((task) => (
                <Row key={task.assignment_id} task={task} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </main>
  );
}

function Row({ task }: { task: MyTask }) {
  const navigate = useNavigate();
  // Prefer the minted submission_id over the handle for shareability;
  // fall back to the handle for newly-granted assignments that haven't
  // had their id minted yet.
  const subId = task.submission_id ?? task.submission_handle;
  const href = `/forms/${encodeURIComponent(task.form_id)}/form/submission/${encodeURIComponent(subId)}`;
  return (
    <tr
      onClick={() => navigate(href)}
      className="cursor-pointer border-b border-border transition-colors hover:bg-surface"
    >
      <td className="py-4 pr-6 align-top">
        <Link
          to={href}
          onClick={(e) => e.stopPropagation()}
          className="font-medium text-ink hover:text-accent"
        >
          {task.form_title}
        </Link>
        <div className="mt-1 font-mono text-xs text-muted">{task.form_id}</div>
      </td>
      <td className="py-4 pr-6 align-top">
        <span className="font-mono text-xs uppercase tracking-wider text-ink">
          {task.role_id}
        </span>
      </td>
      <td className="py-4 pr-6 align-top">
        <StatePill state={task.submission_state} />
      </td>
      <td className="py-4 pr-6 align-top font-mono text-xs text-muted">
        {formatTimestamp(task.granted_at)}
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
        No tasks assigned to you.
      </p>
      <p className="mt-3 font-mono text-xs uppercase tracking-wider text-muted">
        Assignments land here when a colleague's form sends one your way.
      </p>
    </div>
  );
}
