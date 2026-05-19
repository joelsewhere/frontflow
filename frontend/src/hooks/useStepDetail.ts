import { useQuery } from "@tanstack/react-query";
import { getStepDetail, type StepDetail } from "../lib/api";

/**
 * Fetch the step form schema + XCom data for a step. Enabled only
 * when all ids are present.
 */
export function useStepDetail(
  formId: string | undefined,
  submissionId: string | undefined,
  stepId: string | undefined,
) {
  return useQuery<StepDetail>({
    queryKey: ["stepDetail", formId, submissionId, stepId],
    queryFn: () => getStepDetail(formId!, submissionId!, stepId!),
    enabled: Boolean(formId && submissionId && stepId),
    staleTime: 60_000,
  });
}
