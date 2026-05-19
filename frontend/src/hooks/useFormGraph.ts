import { useQuery } from "@tanstack/react-query";
import { getFormGraph, type WorkflowGraph } from "../lib/api";

/**
 * Fetch a form's compiled structural graph — every node and backend
 * step with its inputs, the `>>` execution edges, and the input-level
 * dependency edges. The form summary page renders this as a static
 * DAG.
 *
 * Cached aggressively: a form's structure is fixed by its compiled
 * definition and only changes on backend redeploy.
 */
export function useFormGraph(formId: string | undefined) {
  return useQuery<WorkflowGraph>({
    queryKey: ["formGraph", formId],
    queryFn: () => getFormGraph(formId!),
    enabled: Boolean(formId),
    staleTime: 5 * 60_000,
  });
}
