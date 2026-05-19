import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getSubmission,
  TERMINAL_SUBMISSION_STATES,
  type Submission,
} from "../lib/api";

const POLL_INTERVAL_MS = 2_000;
const SLOW_POLL_INTERVAL_MS = 30_000;

/**
 * Fetches a submission's current state and keeps it fresh.
 *
 * Cross-user staleness mitigations:
 *   1. While the submission is active, poll fast (POLL_INTERVAL_MS).
 *   2. Once it reaches a terminal state, keep polling slowly so a
 *      clear by another user surfaces within ~30s.
 *   3. Refetch on window focus.
 *   4. When a HITL step regresses from success to a non-success state,
 *      invalidate that step's cached detail so the form re-renders.
 */
export function useSubmission(
  formId: string | undefined,
  submissionId: string | undefined,
) {
  const queryClient = useQueryClient();
  const previousDataRef = useRef<Submission | null>(null);

  const query = useQuery<Submission>({
    queryKey: ["submission", formId, submissionId],
    queryFn: () => getSubmission(formId!, submissionId!),
    enabled: Boolean(formId && submissionId),
    refetchInterval: (q) => {
      const state = q.state.data?.state;
      if (state && TERMINAL_SUBMISSION_STATES.has(state)) {
        return SLOW_POLL_INTERVAL_MS;
      }
      return POLL_INTERVAL_MS;
    },
    refetchOnWindowFocus: true,
  });

  useEffect(() => {
    const prev = previousDataRef.current;
    const curr = query.data;
    previousDataRef.current = curr ?? null;
    if (!curr || !prev || !formId || !submissionId) return;

    for (const task of curr.tasks) {
      if (!task.is_hitl) continue;
      const prevTask = prev.tasks.find((t) => t.task_id === task.task_id);
      if (prevTask?.state === "success" && task.state !== "success") {
        queryClient.invalidateQueries({
          queryKey: ["stepDetail", formId, submissionId, task.task_id],
        });
      }
    }
  }, [query.data, formId, submissionId, queryClient]);

  return query;
}
