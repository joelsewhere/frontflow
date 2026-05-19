import { useMutation, useQueryClient } from "@tanstack/react-query";
import { submitStep, type StepDetail } from "../lib/api";

/**
 * Submit a step's form. On success writes the returned StepDetail
 * into cache and invalidates the submission polling query.
 */
export function useSubmitStep(
  formId: string,
  submissionId: string,
  stepId: string,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      values,
      button,
    }: {
      values: Record<string, unknown>;
      button: string | null;
    }) => submitStep(formId, submissionId, stepId, values, button),
    onSuccess: (data: StepDetail) => {
      queryClient.setQueryData(
        ["stepDetail", formId, submissionId, stepId],
        data,
      );
      queryClient.invalidateQueries({
        queryKey: ["submission", formId, submissionId],
      });
    },
  });
}
