import { Navigate, useParams, useSearchParams } from "react-router-dom";

/**
 * Legacy route — the form's submissions list is now a tab on the form
 * summary page. Old `/forms/:formId/submissions` URLs are redirected
 * to `/forms/:formId?tab=submissions`. Any other query params (e.g. a
 * `?submission=` deep link from a shared drawer URL) are preserved.
 */
export default function FormSubmissionsPage() {
  const { formId } = useParams<{ formId: string }>();
  const [params] = useSearchParams();
  if (!formId) return <Navigate to="/forms" replace />;
  const out = new URLSearchParams(params);
  out.set("tab", "submissions");
  return (
    <Navigate
      to={`/forms/${encodeURIComponent(formId)}?${out.toString()}`}
      replace
    />
  );
}
