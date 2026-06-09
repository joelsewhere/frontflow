import { useCallback } from "react";
import { useSearchParams } from "react-router-dom";

export type FormTab = "overview" | "submissions" | "reports" | "theme" | "access" | "source";
export const FORM_TABS: FormTab[] = [
  "overview",
  "submissions",
  "reports",
  "theme",
  "access",
  "source",
];
const DEFAULT_TAB: FormTab = "overview";

/**
 * URL-synced state for the form summary page's tabs — reads and writes
 * the `?tab=` query param. The default tab is Overview when the param
 * is absent or unknown. Composes with the drawer's `?submission=` so
 * opening a submission from the Submissions tab keeps you on that tab.
 */
export function useFormTab(): [FormTab, (tab: FormTab) => void] {
  const [params, setParams] = useSearchParams();
  const raw = params.get("tab");
  const tab: FormTab =
    raw && (FORM_TABS as string[]).includes(raw)
      ? (raw as FormTab)
      : DEFAULT_TAB;

  const setTab = useCallback(
    (next: FormTab) => {
      const out = new URLSearchParams(params);
      if (next === DEFAULT_TAB) {
        // Don't dirty the URL with the default value.
        out.delete("tab");
      } else {
        out.set("tab", next);
      }
      setParams(out, { replace: false });
    },
    [params, setParams],
  );

  return [tab, setTab];
}
