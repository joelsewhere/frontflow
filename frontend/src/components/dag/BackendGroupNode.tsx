import { useState } from "react";
import { StatusIndicator } from "./StatusIndicator";
import { type NodeStatus } from "../../lib/dagState";
import { type TaskInstance } from "../../lib/api";

function humanize(id: string): string {
  return id.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * A `with backend_group(...)` in the chain: several workflow-level
 * backend steps collapsed into one status node. The header carries
 * the group title and an aggregate status — failed if any sub-step
 * failed, running while any is still going, success when all are
 * done — plus a live "current sub-step" line while running. Expand
 * to see every sub-step with its own status (and error detail when
 * one failed).
 */
export function BackendGroupNode({
  tasks,
  title,
  stepLabel,
}: {
  tasks: TaskInstance[];
  title: string;
  stepLabel?: string;
}) {
  const [open, setOpen] = useState(false);

  const anyFailed = tasks.some((t) => t.state === "failed");
  const anyRunning = tasks.some((t) => t.state === "running");
  const status: NodeStatus = anyFailed
    ? "failed"
    : anyRunning
      ? "running"
      : "success";
  const doneCount = tasks.filter((t) => t.state === "success").length;
  const current = tasks.find((t) => t.state === "running");

  const subtitle = anyFailed
    ? "failed"
    : anyRunning
      ? `${humanize(current?.task_id ?? "")}… (${doneCount}/${tasks.length})`
      : `${tasks.length} steps`;

  return (
    <div
      className={[
        "flex flex-col gap-2 border bg-surface rounded-theme px-5 py-3",
        anyFailed ? "border-error" : "border-border",
      ].join(" ")}
      data-status={status}
    >
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex items-center gap-3 text-left cursor-pointer"
      >
        <StatusIndicator status={status} size="sm" />
        <div className="flex flex-col gap-0.5 min-w-0 flex-1">
          <span className="font-mono text-[10px] uppercase tracking-[0.25em] text-muted">
            {stepLabel ? `${stepLabel} · ` : ""}
            Backend group · {subtitle}
          </span>
          <span className="font-display text-sm font-semibold leading-tight text-ink truncate">
            {title}
          </span>
        </div>
        <span
          className={`text-muted text-xs transition-transform ${
            open ? "rotate-90" : ""
          }`}
        >
          ▸
        </span>
      </button>
      {open ? (
        <div className="flex flex-col gap-1.5 pl-7">
          {tasks.map((t) => (
            <div key={t.task_id} className="flex flex-col gap-1">
              <div className="flex items-center gap-2.5">
                <StatusIndicator
                  status={
                    t.state === "failed"
                      ? "failed"
                      : t.state === "running"
                        ? "running"
                        : "success"
                  }
                  size="sm"
                />
                <span className="font-sans text-sm text-ink">
                  {humanize(t.task_id)}
                </span>
              </div>
              {t.state === "failed" && t.error ? (
                <pre className="font-mono text-xs whitespace-pre-wrap break-words text-error pl-7 m-0">
                  {t.error}
                </pre>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
