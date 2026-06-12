import { StatusIndicator } from "./StatusIndicator";
import { type NodeStatus } from "../../lib/dagState";

interface BackendStepNodeProps {
  taskId: string;
  /** Task state from the API: "success" | "failed" | "running". */
  state: string;
  stepLabel?: string;
  /** Per-step error text. Rendered only when state is "failed".
   * Lets the user see the actual exception (e.g. "ValueError: ...")
   * instead of just the bare "failed" label. */
  error?: string | null;
  /** Full Python traceback paired with `error`. Rendered inside a
   * collapsed `<details>` panel below the short message — open it
   * to see the full stack. */
  traceback?: string | null;
}

function humanize(id: string): string {
  return id
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * A backend step in the chain. Unlike a HITL node or a task run, a
 * backend step runs instantly when the workflow reaches it — so it's a
 * compact one-line marker rather than a full card. A failed step is
 * surfaced here in error styling, with the recorded error message
 * shown below so the user doesn't need to dig through server logs to
 * see what went wrong. The full Python traceback (when present) is
 * tucked inside a `<details>` collapsible so the short message stays
 * scannable but the full stack is one click away.
 */
export function BackendStepNode({
  taskId,
  state,
  stepLabel,
  error,
  traceback,
}: BackendStepNodeProps) {
  const failed = state === "failed";
  const status: NodeStatus = failed
    ? "failed"
    : state === "running"
      ? "running"
      : "success";

  return (
    <div
      className={[
        "flex flex-col gap-2 border bg-surface rounded-theme px-5 py-3",
        failed ? "border-error" : "border-border",
      ].join(" ")}
      data-status={status}
    >
      <div className="flex items-center gap-3">
        <StatusIndicator status={status} size="sm" />
        <div className="flex flex-col gap-0.5 min-w-0">
          <span className="font-mono text-[10px] uppercase tracking-[0.25em] text-muted">
            {stepLabel ? `${stepLabel} · ` : ""}
            {failed ? "Backend step · failed" : "Backend step"}
          </span>
          <span className="font-display text-sm font-semibold leading-tight text-ink truncate">
            {humanize(taskId)}
          </span>
        </div>
      </div>
      {failed && error ? (
        <pre className="font-mono text-xs whitespace-pre-wrap break-words text-error pl-7 m-0">
          {error}
        </pre>
      ) : null}
      {failed && traceback ? (
        <details className="pl-7">
          <summary className="font-mono text-[10px] uppercase tracking-[0.25em] text-muted cursor-pointer select-none hover:text-ink">
            Show traceback
          </summary>
          <pre className="font-mono text-xs whitespace-pre-wrap break-words text-muted mt-2 m-0 max-h-96 overflow-auto">
            {traceback}
          </pre>
        </details>
      ) : null}
    </div>
  );
}
