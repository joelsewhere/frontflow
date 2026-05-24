import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createBrowserRouter, Navigate, Outlet, RouterProvider } from "react-router-dom";
import FormsListPage from "./pages/FormsListPage";
import FormSummaryPage from "./pages/FormSummaryPage";
import FormSubmissionsPage from "./pages/FormSubmissionsPage";
import SubmissionDetailPage from "./pages/SubmissionDetailPage";
import LandingPage from "./pages/LandingPage";
import SubmissionPage from "./pages/SubmissionPage";
import FormThemePage from "./pages/FormThemePage";
import ConnectionsPage from "./pages/ConnectionsPage";
import AccessPage from "./pages/AccessPage";
import UsersPage from "./pages/UsersPage";
import LoginPage from "./pages/LoginPage";
import ChangePasswordPage from "./pages/ChangePasswordPage";
import { RequireAuth, RequireAdmin } from "./auth/AuthContext";
import App from "./App";
import { theme } from "./theme/theme";
import { applyTheme } from "./theme/applyTheme";
import { FormThemeProvider } from "./theme/FormThemeProvider";
import "./index.css";

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
          { path: "/forms/:formId", element: <FormSummaryPage /> },
          {
            path: "/forms/:formId/submissions",
            element: <FormSubmissionsPage />,
          },
          {
            path: "/forms/:formId/submissions/:submissionId",
            element: <SubmissionDetailPage />,
          },
          { path: "/forms/:formId/theme", element: <FormThemePage /> },
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
        ],
      },

      // --- Live form -----------------------------------------------
      // Wrapped in FormThemeProvider so these end-user-facing views
      // render with the form's own theme.
      {
        element: <FormThemeProvider />,
        children: [
          { path: "/forms/:formId/form", element: <LandingPage /> },
          {
            path: "/forms/:formId/form/draft",
            element: <SubmissionPage />,
          },
          {
            path: "/forms/:formId/form/draft/:viewId",
            element: <SubmissionPage />,
          },
          {
            path: "/forms/:formId/form/submission/:submissionId",
            element: <SubmissionPage />,
          },
          {
            path: "/forms/:formId/form/submission/:submissionId/:viewId",
            element: <SubmissionPage />,
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
