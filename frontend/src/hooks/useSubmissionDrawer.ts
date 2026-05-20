import { useCallback } from "react";
import { useSearchParams } from "react-router-dom";

/**
 * URL-synced state for the "currently-open submission drawer" — reads
 * and writes the `?submission=` query param. Used by pages that render
 * the SubmissionDrawer over their content.
 *
 * Returns `[submissionId, open, close]`. `open(id)` sets the param,
 * `close()` removes it; both preserve the rest of the URL.
 */
export function useSubmissionDrawer(): [
  string | null,
  (submissionId: string) => void,
  () => void,
] {
  const [params, setParams] = useSearchParams();
  const submissionId = params.get("submission");

  const open = useCallback(
    (id: string) => {
      const next = new URLSearchParams(params);
      next.set("submission", id);
      setParams(next, { replace: false });
    },
    [params, setParams],
  );

  const close = useCallback(() => {
    const next = new URLSearchParams(params);
    next.delete("submission");
    setParams(next, { replace: false });
  }, [params, setParams]);

  return [submissionId, open, close];
}
