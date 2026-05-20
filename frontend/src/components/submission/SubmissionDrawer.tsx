import { Drawer } from "../ui/Drawer";
import { SubmissionSummaryContent } from "./SubmissionSummaryContent";

interface SubmissionDrawerProps {
  formId: string;
  submissionId: string | null;
  onClose: () => void;
}

/**
 * Slide-in panel showing a submission's persisted record on the form
 * admin page. Open when `submissionId` is non-null; closing the drawer
 * is the caller's responsibility (typically clears the URL's
 * `?submission=` param).
 */
export function SubmissionDrawer({
  formId,
  submissionId,
  onClose,
}: SubmissionDrawerProps) {
  const open = submissionId !== null;
  return (
    <Drawer open={open} onClose={onClose}>
      {/* Header — close button + breadcrumb */}
      <header className="flex items-center justify-between border-b border-border px-6 py-4">
        <p className="font-mono text-[11px] uppercase tracking-[0.18em] text-muted">
          Submission
        </p>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="font-mono text-xs uppercase tracking-wider text-muted hover:text-ink"
        >
          Close ✕
        </button>
      </header>
      {/* Scrollable body */}
      <div className="grow overflow-y-auto px-6 py-6">
        {submissionId !== null ? (
          <SubmissionSummaryContent
            formId={formId}
            submissionId={submissionId}
          />
        ) : null}
      </div>
    </Drawer>
  );
}
