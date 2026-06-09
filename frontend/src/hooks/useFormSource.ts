import { useQuery } from "@tanstack/react-query";
import { getFormSource, getFormVersionSource, type FormSource } from "../lib/api";

/**
 * Fetch the raw Python source for a form's LIVE version — the
 * `.py` file the currently-loaded workflow was compiled from.
 * Admin-only on the backend; non-admins see a 403 which surfaces
 * as a thrown error to the consumer.
 *
 * Cached aggressively — a form's source only changes on backend
 * redeploy (or a successful `POST /api/refresh` after an S3
 * upload). When that happens the cache is invalidated explicitly
 * by the refresh trigger; otherwise the source is effectively
 * immutable.
 */
export function useFormSource(formId: string | undefined) {
  return useQuery<FormSource>({
    queryKey: ["formSource", formId],
    queryFn: () => getFormSource(formId!),
    enabled: Boolean(formId),
    staleTime: 5 * 60_000,
  });
}

/**
 * Fetch the source PINNED to a specific form_version — the exact
 * code a submission was running, even if the live form has since
 * been edited and bumped past that version. Used by
 * SubmissionDetailPage's Source tab so an investigator looking at
 * an old submission sees the code that actually ran.
 */
export function useFormVersionSource(
  formId: string | undefined,
  version: number | undefined,
) {
  return useQuery<FormSource>({
    queryKey: ["formVersionSource", formId, version],
    queryFn: () => getFormVersionSource(formId!, version!),
    enabled: Boolean(formId) && typeof version === "number",
    // A pinned version's source is immutable — cache forever within
    // the session.
    staleTime: Infinity,
  });
}
