import { useQuery } from "@tanstack/react-query";
import { getFormSubmissions, type SubmissionSummary } from "../lib/api";

/**
 * Fetch a form's submissions for the tracking list — newest first.
 *
 * Refetched on window focus; no active polling, so a running
 * submission's state won't live-update while the list sits open.
 */
export function useFormSubmissions(formId: string | undefined) {
  return useQuery<SubmissionSummary[]>({
    queryKey: ["formSubmissions", formId],
    queryFn: () => getFormSubmissions(formId!),
    enabled: Boolean(formId),
    refetchOnWindowFocus: true,
    staleTime: 30_000,
  });
}
