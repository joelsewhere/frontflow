/**
 * Where the form-filling pages get their route inputs.
 *
 * By default: react-router, exactly as before — the pages behave
 * identically on their real routes.
 *
 * Inside a workspace panel a form cannot use the browser's route: the
 * dock shows several panels at once, and react-router v7 forbids
 * nesting a second <Router> to give each one its own. So a panel
 * supplies an override — a private path it keeps in state — and the
 * pages read from that instead.
 *
 * The react-router hooks are still called unconditionally here, which is
 * safe: a panel is rendered inside the application's Router, so they
 * resolve (to the workspace's own route) rather than throwing. Only
 * which value wins changes.
 */

import { createContext, useContext } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";

export interface FormRoutingValue {
  formId?: string;
  submissionId?: string;
  viewId?: string;
  /** Router state — carries the draft `handle`, which has no URL slot. */
  state: unknown;
  navigate: (to: string, options?: { replace?: boolean; state?: unknown }) => void;
}

const FormRoutingContext = createContext<FormRoutingValue | null>(null);

export const FormRoutingProvider = FormRoutingContext.Provider;

export function useFormRouting(): FormRoutingValue {
  const override = useContext(FormRoutingContext);

  // Always called — hooks cannot be conditional, and inside a workspace
  // panel these resolve against the app's router rather than throwing.
  const params = useParams<{
    formId: string;
    submissionId: string;
    viewId: string;
  }>();
  const location = useLocation();
  const navigate = useNavigate();

  if (override) return override;

  return {
    formId: params.formId,
    submissionId: params.submissionId,
    viewId: params.viewId,
    state: location.state,
    navigate: (to, options) => navigate(to, options),
  };
}

/**
 * Parse one of the live-form paths back into its parts.
 *
 * A workspace panel navigates by path string — the pages build those
 * paths themselves — so the panel has to read them back out. Kept beside
 * the context so the two stay in step with the routes in main.tsx.
 *
 *   /forms/:formId/form
 *   /forms/:formId/form/draft[/:viewId]
 *   /forms/:formId/form/submission/:submissionId[/:viewId]
 */
export function parseFormPath(path: string): {
  formId?: string;
  submissionId?: string;
  viewId?: string;
} {
  const clean = path.split("?")[0].replace(/\/+$/, "");
  const parts = clean.split("/").filter(Boolean);
  // ["forms", formId, "form", ...rest]
  if (parts[0] !== "forms" || parts[2] !== "form") return {};

  const formId = decodeURIComponent(parts[1] ?? "");
  const rest = parts.slice(3);

  if (rest[0] === "draft") {
    return { formId, viewId: rest[1] ? decodeURIComponent(rest[1]) : undefined };
  }
  if (rest[0] === "submission") {
    return {
      formId,
      submissionId: rest[1] ? decodeURIComponent(rest[1]) : undefined,
      viewId: rest[2] ? decodeURIComponent(rest[2]) : undefined,
    };
  }
  return { formId };
}
