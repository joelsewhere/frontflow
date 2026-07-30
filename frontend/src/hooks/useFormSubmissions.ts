import { useQuery, keepPreviousData } from "@tanstack/react-query";
import {
  getFormSubmissions,
  type SubmissionListingPage,
  type SubmissionListingQuery,
} from "../lib/api";

/**
 * Fetch one page of a form's submission listing for the tracking
 * tab. Server-paginated, server-filtered, server-sorted — the
 * `query` argument carries the user's choices and the result is
 * the page envelope.
 *
 * `keepPreviousData` keeps the prior page visible while a new
 * page is loading: paginating, sorting, or filtering wouldn't
 * otherwise flash an empty table for the duration of the request.
 *
 * Refetched on window focus; no active polling, so a running
 * submission's state won't live-update while the list sits open.
 */
export function useFormSubmissions(
  formId: string | undefined,
  query: SubmissionListingQuery = {},
) {
  return useQuery<SubmissionListingPage>({
    // The query key folds in every parameter so React Query
    // refetches when ANY of them changes — pagination, filter,
    // or sort. Order matters for sort (a different order is a
    // different page set), so we serialize sort entries directly
    // rather than treating them as a Set.
    queryKey: ["formSubmissions", formId, query],
    queryFn: () => getFormSubmissions(formId!, query),
    enabled: Boolean(formId),
    refetchOnWindowFocus: true,
    staleTime: 30_000,
    placeholderData: keepPreviousData,
  });
}
