import { useMutation } from "@tanstack/react-query";
import { startSubmission, type Submission } from "../lib/api";

/**
 * Wraps the start-submission mutation. Components consume `mutate`,
 * `isPending`, `data`, and `error` — no need to import TanStack Query
 * directly.
 *
 * For now the formId is hardcoded by the caller; when multi-form lands
 * it'll come from the URL / form picker.
 */
export function useStartSubmission(
  formId: string,
  onSuccess?: (data: Submission) => void,
) {
  return useMutation({
    mutationFn: (values: Record<string, unknown>) =>
      startSubmission(formId, values),
    onSuccess,
  });
}
