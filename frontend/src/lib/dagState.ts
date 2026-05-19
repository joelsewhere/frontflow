/**
 * NodeStatus is our internal vocabulary for visual node states. Airflow
 * has more granular task states; we collapse them to what the UI needs.
 *
 * - `pending` — not yet started (queued / scheduled / up_for_reschedule)
 * - `running` — actively executing
 * - `success` — done, succeeded
 * - `failed`  — done, errored
 * - `waiting` — paused for human input (HITL deferred). Step 5+.
 */
export type NodeStatus =
  | "pending"
  | "running"
  | "success"
  | "failed"
  | "waiting";

/**
 * Map an Airflow task/dag state string onto a NodeStatus.
 * Conservative: unknown states fall through to "pending".
 */
export function toNodeStatus(airflowState: string): NodeStatus {
  switch (airflowState) {
    case "running":
    case "restarting":
      return "running";
    case "success":
      return "success";
    case "failed":
    case "upstream_failed":
      return "failed";
    case "deferred":
    case "awaiting_response":
      return "waiting";
    case "queued":
    case "scheduled":
    case "up_for_reschedule":
    case "up_for_retry":
    case "none":
      return "pending";
    default:
      return "pending";
  }
}
