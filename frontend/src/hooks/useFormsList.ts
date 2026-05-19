import { useQuery } from "@tanstack/react-query";
import { listForms, type FormSummary } from "../lib/api";

/**
 * Fetch the forms index — every form with its folder, version count,
 * and submission counts by state.
 *
 * Refetched on window focus so the counts stay reasonably fresh when
 * you come back to the tab; there's no active polling, so the list
 * won't update while you sit and watch it.
 */
export function useFormsList() {
  return useQuery<FormSummary[]>({
    queryKey: ["formsList"],
    queryFn: listForms,
    refetchOnWindowFocus: true,
    staleTime: 30_000,
  });
}
