import { StatusIndicator } from "./StatusIndicator";
import { type NodeStatus } from "../../lib/dagState";

interface BackendStepNodeProps {
  taskId: string;
  /** Task state from the API: "success" | "failed" | "running". */
  state: string;
  stepLabel?: string;
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
 * surfaced here in error styling.
 */
export function BackendStepNode({
  taskId,
  state,
  stepLabel,
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
        "flex items-center gap-3 border bg-surface rounded-theme px-5 py-3",
        failed ? "border-error" : "border-border",
      ].join(" ")}
      data-status={status}
    >
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
  );
}
