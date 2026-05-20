import { Navigate, useParams } from "react-router-dom";

/**
 * Legacy route — the theme editor is now a tab on the form summary
 * page. Old `/forms/:formId/theme` URLs are redirected to
 * `/forms/:formId?tab=theme`, preserving bookmarks and external links.
 */
export default function FormThemePage() {
  const { formId } = useParams<{ formId: string }>();
  if (!formId) return <Navigate to="/forms" replace />;
  return (
    <Navigate
      to={`/forms/${encodeURIComponent(formId)}?tab=theme`}
      replace
    />
  );
}
