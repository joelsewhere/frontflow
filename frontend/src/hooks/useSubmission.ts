import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getSubmission,
  TERMINAL_SUBMISSION_STATES,
  type Submission,
} from "../lib/api";

const FAST_POLL_INTERVAL_MS = 2_000;
const MODERATE_POLL_INTERVAL_MS = 10_000;
const SLOW_POLL_INTERVAL_MS = 30_000;

/**
 * Fetches a submission's current state and keeps it fresh.
 *
 * Polling is tiered by what the submission is actually doing:
 *   - Terminal (success / failed) → slow poll (30s). Keeps an
 *     eye out for a clear by another user without much traffic.
 *   - In-flight with an *active Airflow operator* (any task of
 *     kind "external" not yet succeeded or failed) → fast poll.
 *     The interval is the MIN of in-flight operators' declared
 *     `poll_interval_ms` (an operator that wants slower polling
 *     can declare it), falling back to FAST_POLL_INTERVAL_MS
 *     when an operator doesn't specify. Airflow has no push
 *     channel, so progress only shows up via polling.
 *   - In-flight with no active externals — a pure human-input
 *     step → moderate poll (10s). Nothing on the backend can
 *     change without a request, so the only reason to re-fetch
 *     is to catch cross-user activity, which 10s covers fine.
 *
 * Refetch on window focus stays on for the "came back to the tab
 * after lunch" case. A HITL step regressing from success to a
 * non-success state invalidates that step's cached detail so the
 * form re-renders.
 *
 * Fast-tier intervals are operator-configurable via the operator's
 * `poll_interval_ms` constructor arg. The moderate and slow tiers
 * stay framework constants — they cover submission-level concerns
 * (cross-user activity, terminal-state monitoring) that don't have
 * a per-operator analog.
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
      const data = q.state.data;
      const state = data?.state;
      if (state && TERMINAL_SUBMISSION_STATES.has(state)) {
        return SLOW_POLL_INTERVAL_MS;
      }
      // External tasks not yet terminal mean an Airflow operator is
      // in flight — only polling surfaces its progress. Take the
      // MIN of their declared `poll_interval_ms` so the fastest
      // operator's preferred rate sets the floor; an operator that
      // didn't declare one falls back to FAST_POLL_INTERVAL_MS. If
      // no externals are in flight, fall back to MODERATE.
      const activeExternalRates =
        data?.tasks
          ?.filter(
            (t) =>
              t.kind === "external" &&
              !TERMINAL_SUBMISSION_STATES.has(t.state),
          )
          .map((t) => t.poll_interval_ms ?? FAST_POLL_INTERVAL_MS) ?? [];
      if (activeExternalRates.length === 0) {
        return MODERATE_POLL_INTERVAL_MS;
      }
      return Math.min(...activeExternalRates);
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
