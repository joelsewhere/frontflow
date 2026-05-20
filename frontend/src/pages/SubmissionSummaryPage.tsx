import { Navigate, useParams } from "react-router-dom";

/**
 * Legacy route — the standalone submission summary page is retired in
 * favor of a drawer rendered on the form summary page. Old
 * `/forms/:formId/submissions/:submissionId` URLs are redirected to
 * `/forms/:formId?submission=:submissionId`, which opens the drawer
 * pre-populated. Bookmarks and shared links keep working.
 */
export default function SubmissionSummaryPage() {
  const { formId, submissionId } = useParams<{
    formId: string;
    submissionId: string;
  }>();
  if (!formId || !submissionId) {
    return <Navigate to="/forms" replace />;
  }
  return (
    <Navigate
      to={`/forms/${encodeURIComponent(formId)}?submission=${encodeURIComponent(submissionId)}`}
      replace
    />
  );
}
