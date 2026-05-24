import { useQuery } from "@tanstack/react-query";
import { getSubmissionDetail, type SubmissionDetail } from "../lib/api";

/**
 * Fetch one submission's persisted record — its steps (with the data
 * each captured) and its event history. Powers the submission summary
 * page; distinct from `useSubmission`, which drives the live run view.
 */
export function useSubmissionDetail(
  formId: string | undefined,
  submissionId: string | undefined,
  versionId?: number,
) {
  return useQuery<SubmissionDetail>({
    queryKey: ["submissionDetail", formId, submissionId, versionId],
    queryFn: () => getSubmissionDetail(formId!, submissionId!, versionId),
    enabled: Boolean(formId && submissionId),
    refetchOnWindowFocus: true,
    staleTime: 30_000,
  });
}
