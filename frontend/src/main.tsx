import React, { Suspense } from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createBrowserRouter, Navigate, Outlet, RouterProvider } from "react-router-dom";
import FormsListPage from "./pages/FormsListPage";
// Lazy-loaded pages — each carries a heavy vendor (graph-vendor for
// the form/submission graph views, charts-vendor + markdown-vendor for
// FormThemePage's analytics + display previews). Splitting them out
// of the initial entry means the user lands on /forms (the most
// common entry point) without paying for code they may never reach.
const FormSummaryPage = React.lazy(() => import("./pages/FormSummaryPage"));
const FormSubmissionsPage = React.lazy(() => import("./pages/FormSubmissionsPage"));
const SubmissionDetailPage = React.lazy(() => import("./pages/SubmissionDetailPage"));
const SubmissionPage = React.lazy(() => import("./pages/SubmissionPage"));
const FormThemePage = React.lazy(() => import("./pages/FormThemePage"));
const LandingPage = React.lazy(() => import("./pages/LandingPage"));
import ConnectionsPage from "./pages/ConnectionsPage";
import AccessPage from "./pages/AccessPage";
import UsersPage from "./pages/UsersPage";
import UserDetailPage from "./pages/UserDetailPage";
import MyTasksPage from "./pages/MyTasksPage";
import LoginPage from "./pages/LoginPage";
import ChangePasswordPage from "./pages/ChangePasswordPage";
import { RequireAuth, RequireAdmin } from "./auth/AuthContext";
import App from "./App";
import { theme } from "./theme/theme";
import { applyTheme } from "./theme/applyTheme";
import { FormThemeProvider } from "./theme/FormThemeProvider";
import "./index.css";

/**
 * Wrap a lazy-loaded route element in a Suspense boundary with a
 * neutral fallback. The fallback is intentionally minimal — a
 * full-page skeleton would flash on every navigation between
 * lazy-loaded routes, which is more visual noise than the brief
 * blank moment while the chunk fetches.
 */
function L(node: React.ReactNode): React.ReactNode {
  return (
    <Suspense fallback={<div className="min-h-screen bg-bg" />}>
      {node}
    </Suspense>
  );
}

// Apply the theme before React renders so the first paint uses the
// runtime CSS variables (not just the parse-time defaults in theme.css).
applyTheme(theme);

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false },
  },
});

// Routing splits into two zones:
//   Console   — /forms, /forms/:id, /forms/:id/submissions[/:sid] — the
//               builder/management surface (overviews + tracking).
//   Live form — /forms/:id/form/* — the end-user-facing form app, and
//               what the embeddable iframe will eventually serve.
const router = createBrowserRouter([
  {
    element: <App />,
    children: [
      // Root redirects to the forms index for now. Becomes a real home
      // screen (high-level reporting) later — swap the Navigate then.
      { path: "/", element: <Navigate to="/forms" replace /> },

      // --- Login (public) ------------------------------------------
      { path: "/login", element: <LoginPage /> },

      // --- Console (admin — requires sign-in) ----------------------
      {
        element: (
          <RequireAuth>
            <Outlet />
          </RequireAuth>
        ),
        children: [
          { path: "/forms", element: <FormsListPage /> },
          { path: "/forms/:formId", element: L(<FormSummaryPage />) },
          {
            path: "/forms/:formId/submissions",
            element: L(<FormSubmissionsPage />),
          },
          {
            path: "/forms/:formId/submissions/:submissionId",
            element: L(<SubmissionDetailPage />),
          },
          { path: "/forms/:formId/theme", element: L(<FormThemePage />) },
          // /my-tasks — every signed-in user's personal inbox of
          // SubmissionAssignments granted to them.
          { path: "/my-tasks", element: <MyTasksPage /> },
          // Reachable by a forced-change user — RequireAuth lets the
          // change-password path through its funnel.
          {
            path: "/change-password",
            element: <ChangePasswordPage />,
          },
        ],
      },

      // --- Admin-only console pages --------------------------------
      {
        element: (
          <RequireAdmin>
            <Outlet />
          </RequireAdmin>
        ),
        children: [
          { path: "/connections", element: <ConnectionsPage /> },
          { path: "/access", element: <AccessPage /> },
          { path: "/users", element: <UsersPage /> },
          { path: "/users/:userId", element: <UserDetailPage /> },
        ],
      },

      // --- Live form -----------------------------------------------
      // Wrapped in FormThemeProvider so these end-user-facing views
      // render with the form's own theme.
      {
        element: <FormThemeProvider />,
        children: [
          { path: "/forms/:formId/form", element: L(<LandingPage />) },
          {
            path: "/forms/:formId/form/draft",
            element: L(<SubmissionPage />),
          },
          {
            path: "/forms/:formId/form/draft/:viewId",
            element: L(<SubmissionPage />),
          },
          {
            path: "/forms/:formId/form/submission/:submissionId",
            element: L(<SubmissionPage />),
          },
          {
            path: "/forms/:formId/form/submission/:submissionId/:viewId",
            element: L(<SubmissionPage />),
          },
        ],
      },
    ],
  },
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </React.StrictMode>,
);
