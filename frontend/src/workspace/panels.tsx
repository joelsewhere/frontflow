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

import { Suspense, lazy } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { DashboardEmbed } from "../components/blocks/DashboardBlock";
import { FormThemeProvider } from "../theme/FormThemeProvider";

const LandingPage = lazy(() => import("../pages/LandingPage"));
const SubmissionPage = lazy(() => import("../pages/SubmissionPage"));

function Loading() {
  return <div className="p-4 text-sm text-muted">Loading…</div>;
}

export function WorkspaceFormPanel({ formId }: { formId: string }) {
  return (
    <div className="h-full overflow-auto bg-bg">
      <Suspense fallback={<Loading />}>
        <MemoryRouter initialEntries={[`/forms/${formId}/form`]}>
          <Routes>
            {/* FormThemeProvider wraps these in main.tsx too — the
                end-user views expect the form's own theme tokens. */}
            <Route element={<FormThemeProvider />}>
              <Route path="/forms/:formId/form" element={<LandingPage />} />
              <Route
                path="/forms/:formId/form/draft"
                element={<SubmissionPage />}
              />
              <Route
                path="/forms/:formId/form/draft/:viewId"
                element={<SubmissionPage />}
              />
              <Route
                path="/forms/:formId/form/submission/:submissionId"
                element={<SubmissionPage />}
              />
              <Route
                path="/forms/:formId/form/submission/:submissionId/:viewId"
                element={<SubmissionPage />}
              />
            </Route>
          </Routes>
        </MemoryRouter>
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
