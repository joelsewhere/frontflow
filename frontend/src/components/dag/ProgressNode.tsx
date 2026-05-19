import { type TaskInstance } from "../../lib/api";
import { toNodeStatus, type NodeStatus } from "../../lib/dagState";
import { DagNode } from "./DagNode";
import { StatusIndicator } from "./StatusIndicator";
import { ResetButton } from "./ResetButton";

interface ProgressNodeProps {
  /** The tasks in this segment, in execution order. */
  tasks: TaskInstance[];
  /** Form ID — needed for the nested clear endpoint URL. */
  formId: string;
  /** Submission ID — needed so per-task reset can call clear. */
  submissionId: string;
  stepLabel?: string;
}

/**
 * Visual node for one segment of the DAG — a contiguous run of non-HITL
 * tasks. Derives its overall status from the tasks themselves: any
 * running → "running"; all success → "success"; any failed → "failed";
 * otherwise "pending".
 *
 * Each task in the segment that's in a terminal state (success or
 * failed) gets a small reset icon — clicking it clears that task and
 * all downstream tasks.
 */
export function ProgressNode({ tasks, formId, submissionId, stepLabel }: ProgressNodeProps) {
  const status = deriveSegmentStatus(tasks);
  const featured = pickFeaturedTask(tasks, status);
  // The HITL node leads the segment; its status flags an upstream edit.
  const cascadeStatus = tasks.find((t) => t.is_hitl)?.status;

  return (
    <DagNode
      status={status}
      stepLabel={stepLabel}
      cascadeStatus={cascadeStatus}
      title={titleFor(status)}
      subtitle={subtitleFor(status, featured, tasks.length)}
    >
      <ul className="flex flex-col gap-2.5">
        {tasks.map((task) => {
          const taskStatus = toNodeStatus(task.state);
          // A terminal task can be rerun — but only if the step itself
          // permits it (an operator/backend authored retryable=false
          // suppresses its own rerun menu).
          const canReset =
            (taskStatus === "success" || taskStatus === "failed") &&
            task.retryable;
          return (
            <li
              key={task.task_id}
              className="flex flex-col gap-1"
            >
              <span className="flex items-center justify-between gap-4">
                <span className="flex items-center gap-3 min-w-0">
                  <StatusIndicator status={taskStatus} size="sm" />
                  <span
                    className={[
                      "font-mono text-sm truncate",
                      taskStatus === "success" ? "text-muted" : "text-ink",
                    ].join(" ")}
                  >
                    {task.task_id}
                  </span>
                </span>
                <span className="flex items-center gap-2 shrink-0">
                  <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted">
                    {task.state}
                  </span>
                  {canReset ? (
                    <ResetButton
                      formId={formId}
                      submissionId={submissionId}
                      fromTaskId={task.task_id}
                      variant="icon"
                      allowScopeChoice
                    />
                  ) : null}
                </span>
              </span>
              {taskStatus === "failed" && task.detail ? (
                <span className="pl-[26px] font-mono text-xs text-error">
                  {task.detail}
                </span>
              ) : null}
            </li>
          );
        })}
      </ul>
    </DagNode>
  );
}

function deriveSegmentStatus(tasks: TaskInstance[]): NodeStatus {
  const statuses = tasks.map((t) => toNodeStatus(t.state));
  if (statuses.some((s) => s === "failed")) return "failed";
  if (statuses.some((s) => s === "running")) return "running";
  if (statuses.length > 0 && statuses.every((s) => s === "success")) {
    return "success";
  }
  return "pending";
}

function pickFeaturedTask(
  tasks: TaskInstance[],
  status: NodeStatus,
): TaskInstance | undefined {
  if (status === "running") {
    return tasks.find((t) => toNodeStatus(t.state) === "running");
  }
  return tasks[tasks.length - 1];
}

function titleFor(status: NodeStatus): string {
  switch (status) {
    case "running":
      return "Processing";
    case "success":
      return "Complete";
    case "failed":
      return "Failed";
    case "pending":
      return "Queued";
    case "waiting":
      return "Awaiting input";
  }
}

function subtitleFor(
  status: NodeStatus,
  featured: TaskInstance | undefined,
  count: number,
): string | undefined {
  if (status === "running" && featured) {
    return featured.task_id.replaceAll("_", " ");
  }
  if (status === "success") {
    return `${count} ${count === 1 ? "task" : "tasks"} succeeded`;
  }
  if (status === "failed" && featured) {
    return `${featured.task_id} failed`;
  }
  return undefined;
}
