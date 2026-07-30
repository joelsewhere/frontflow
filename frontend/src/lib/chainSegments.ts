import { type TaskInstance } from "./api";

/**
 * A segment of the DAG chain. Either:
 *   - "tasks":   a contiguous run of external task instances → ProgressNode
 *   - "hitl":    a single HITL task instance                → HitlNode
 *   - "backend": a single backend-step task instance        → BackendStepNode
 */
export type ChainSegment =
  | { kind: "tasks"; tasks: TaskInstance[] }
  | { kind: "hitl"; task: TaskInstance }
  | { kind: "airflowHitl"; task: TaskInstance }
  | { kind: "backend"; task: TaskInstance }
  | {
      kind: "backendGroup";
      groupId: string;
      title: string;
      tasks: TaskInstance[];
    };

/**
 * Walks the task list in execution order and splits it into renderable
 * segments. HITL nodes and backend steps each become their own segment;
 * external tasks accumulate into adjacent task-run segments.
 *
 * Example:
 *   [validate_input, fetch_records, request_details, greet, summarize]
 *
 *   → tasks   [validate_input, fetch_records]
 *     hitl    request_details
 *     backend greet
 *     tasks   [summarize]
 *
 * Tasks that haven't yet entered the run won't appear, by design — the
 * chain grows as the runtime advances.
 */
export function buildChainSegments(tasks: TaskInstance[]): ChainSegment[] {
  const segments: ChainSegment[] = [];
  let buffer: TaskInstance[] = [];

  const flush = () => {
    if (buffer.length > 0) {
      segments.push({ kind: "tasks", tasks: buffer });
      buffer = [];
    }
  };

  for (const task of tasks) {
    if (task.kind === "hitl") {
      flush();
      segments.push({ kind: "hitl", task });
    } else if (task.kind === "backend") {
      flush();
      // Consecutive backend steps sharing a group_id collapse into a
      // single group segment (a `with backend_group(...)` in the DSL).
      const last = segments[segments.length - 1];
      if (task.group_id && last?.kind === "backendGroup" && last.groupId === task.group_id) {
        last.tasks.push(task);
      } else if (task.group_id) {
        segments.push({
          kind: "backendGroup",
          groupId: task.group_id,
          title: task.group_title ?? task.group_id,
          tasks: [task],
        });
      } else {
        segments.push({ kind: "backend", task });
      }
    } else if (task.is_hitl && task.state === "awaiting_response") {
      // An Airflow HITL operator waiting on a person — its own
      // interactive segment, not a row in a progress node.
      flush();
      segments.push({ kind: "airflowHitl", task });
    } else {
      buffer.push(task);
    }
  }
  flush();

  return segments;
}
