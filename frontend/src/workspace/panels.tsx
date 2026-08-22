/**
 * The two panel kinds a workspace can hold.
 *
 * A form panel reuses the real form-filling pages rather than a
 * reimplementation, so a submission started in a workspace is the same
 * submission as anywhere else — same routes, same state, same history.
 *
 * Those pages are bound to react-router (useParams, useNavigate,
 * useLocation), so each panel gets its own MemoryRouter. The hooks work
 * unchanged, and each panel navigates independently: starting a
 * submission in one panel does not move the browser URL or disturb the
 * others. The routes below mirror the live-form routes in main.tsx —
 * they have to, because the pages navigate between them by path.
 */

import { Suspense, lazy, useCallback, useMemo, useState } from "react";

import { DashboardEmbed } from "../components/blocks/DashboardBlock";
import {
  FormRoutingProvider,
  parseFormPath,
  type FormRoutingValue,
} from "../lib/formRouting";
import { FormThemeProvider } from "../theme/FormThemeProvider";

const LandingPage = lazy(() => import("../pages/LandingPage"));
const SubmissionPage = lazy(() => import("../pages/SubmissionPage"));

function Loading() {
  return <div className="p-4 text-sm text-muted">Loading…</div>;
}

/**
 * A form, filled inside a dock panel.
 *
 * The panel keeps its own path in state and feeds it to the form pages
 * through FormRoutingProvider. A nested <Router> is not an option —
 * react-router v7 refuses to render one inside another — and an iframe
 * is not either: frontflow serves `frame-ancestors 'none'` unless a form
 * opts in via `iframe_allowed_origins`, which only applies to *public*
 * forms. Framing would therefore have forced every workspace form to be
 * published, contradicting the access model.
 *
 * Each panel navigates independently, and the browser URL never moves —
 * which is what you want when four forms are open at once.
 */
export function WorkspaceFormPanel({ formId }: { formId: string }) {
  const [path, setPath] = useState(`/forms/${encodeURIComponent(formId)}/form`);
  const [state, setState] = useState<unknown>(null);

  const navigate = useCallback(
    (to: string, options?: { replace?: boolean; state?: unknown }) => {
      setPath(to);
      setState(options?.state ?? null);
    },
    [],
  );

  const parsed = useMemo(() => parseFormPath(path), [path]);

  const routing: FormRoutingValue = useMemo(
    () => ({
      // The panel's form always wins: a malformed path must not let a
      // panel start rendering some other form.
      formId,
      submissionId: parsed.submissionId,
      viewId: parsed.viewId,
      state,
      navigate,
    }),
    [formId, parsed.submissionId, parsed.viewId, state, navigate],
  );

  // Landing until the form has been started; the submission view after.
  const started = path.includes("/form/draft") || path.includes("/form/submission");

  return (
    <div className="h-full overflow-auto bg-bg">
      <Suspense fallback={<Loading />}>
        <FormRoutingProvider value={routing}>
          {/* The end-user views expect the form's own theme tokens, the
              same as on their real routes. */}
          <FormThemeProvider formId={formId}>
            {started ? <SubmissionPage /> : <LandingPage />}
          </FormThemeProvider>
        </FormRoutingProvider>
      </Suspense>
    </div>
  );
}

export function WorkspaceDashboardPanel({
  workspaceId,
  name,
  showFilters,
}: {
  workspaceId: string;
  name: string;
  showFilters: boolean;
}) {
  return (
    <div className="h-full overflow-hidden bg-bg p-2">
      {/* `fill` rather than a fixed height: a dock panel sizes itself,
          unlike a form layout where the page scrolls and a dashboard
          needs an explicit height. */}
      <DashboardEmbed
        workspaceId={workspaceId}
        formId={null}
        submissionId={null}
        name={name}
        height={0}
        showFilters={showFilters}
        fill
      />
    </div>
  );
}
