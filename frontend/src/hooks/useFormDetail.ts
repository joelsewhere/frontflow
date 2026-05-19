import { useQuery } from "@tanstack/react-query";
import { getFormDetail, type FormDetail } from "../lib/api";

/**
 * Fetch a form's pre-submission schema — title, description, and the
 * landing step's fields + submit button label. The landing page uses
 * this to render everything dynamically from the workflow definition
 * rather than hardcoding form-specific UI.
 *
 * Cached aggressively. A form's schema doesn't change at runtime;
 * the source of truth is the @form / @landing definitions, which
 * only change on backend redeploy.
 */
export function useFormDetail(formId: string | undefined) {
  return useQuery<FormDetail>({
    queryKey: ["formDetail", formId],
    queryFn: () => getFormDetail(formId!),
    enabled: Boolean(formId),
    staleTime: 5 * 60_000,
  });
}
