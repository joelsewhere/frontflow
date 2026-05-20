import { type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError } from "../lib/api";
import { formatTimestamp } from "../lib/format";
import { useFormDetail } from "../hooks/useFormDetail";
import { useFormGraph } from "../hooks/useFormGraph";
import { useFormsList } from "../hooks/useFormsList";
import { useFormTab, type FormTab } from "../hooks/useFormTab";
import { useSubmissionDrawer } from "../hooks/useSubmissionDrawer";
import { ReparseButton } from "../components/listing/ReparseButton";
import { WorkflowGraphCanvas } from "../components/graph/WorkflowGraphCanvas";
import { VisibilityControl } from "../components/console/VisibilityControl";
import { SubmissionDrawer } from "../components/submission/SubmissionDrawer";
import { SubmissionsTab } from "../components/form/SubmissionsTab";
import { ThemeTab } from "../components/form/ThemeTab";
import { Tabs } from "../components/ui/Tabs";

/**
 * Form summary (`/forms/:formId`) — a form's console home: identity,
 * description, submission/version stats, and the primary actions, with
 * the rest of the form's surface organized into tabs:
 *   - Overview: the workflow graph (default)
 *   - Submissions: paginated full list, drawer for the detail
 *   - Theme: the form's end-user styling editor
 *   - Access: visibility / permission grants
 *
 * The active tab is in `?tab=`; deep links and bookmarks survive. The
 * Submissions row click opens the submission drawer via `?submission=`,
 * which composes with the tab param so the drawer can be opened from
 * the Submissions tab without losing its place.
 */
export default function FormSummaryPage() {
  const { formId } = useParams<{ formId: string }>();
  const { data: forms, error: formsError, isLoading } = useFormsList();
  const { data: detail } = useFormDetail(formId);
  const [openSubmissionId, openSubmission, closeSubmission] =
    useSubmissionDrawer();
  const [tab, setTab] = useFormTab();

  const summary = forms?.find((f) => f.form_id === formId);
  const title = detail?.title ?? summary?.name ?? formId ?? "Form";

  return (
    <main className="relative z-10 mx-auto max-w-7xl px-6 pt-24 pb-16">
      <header className="mb-10">
        <Link
          to="/forms"
          className="font-mono text-xs uppercase tracking-wider text-muted hover:text-accent"
        >
          ← All forms
        </Link>
        <div className="mt-4 flex items-start justify-between gap-6">
          <h1 className="font-display text-5xl font-bold leading-[1.0] text-ink">
            {title}
          </h1>
          {formId ? <ReparseButton formId={formId} /> : null}
        </div>
        <div className="mt-3 flex items-center gap-2">
          <span className="font-mono text-xs text-muted">{formId}</span>
          {summary?.folder_path ? (
            <span className="font-mono text-xs text-muted">
              · {summary.folder_path.split("/").join(" / ")}
            </span>
          ) : null}
          {summary && !summary.is_live ? (
            <span className="border border-border px-1.5 font-mono text-[10px] uppercase tracking-wider text-muted">
              Archived
            </span>
          ) : null}
        </div>
        {detail?.description ? (
          <p className="mt-5 max-w-xl font-sans text-base leading-relaxed text-muted">
            {detail.description}
          </p>
        ) : null}
      </header>

      {isLoading ? (
        <p className="font-display text-2xl text-ink opacity-30">Loading…</p>
      ) : formsError ? (
        <div className="border border-error bg-surface p-6">
          <p className="text-error text-sm">
            Couldn't load this form:{" "}
            {formsError instanceof ApiError
              ? formsError.message
              : "unknown error"}
          </p>
        </div>
      ) : !summary ? (
        <div className="border border-error bg-surface p-6">
          <p className="text-error text-sm">
            No form with id {formId} exists.
          </p>
        </div>
      ) : (
        <>
          <section className="flex border-y border-border">
            <Stat label="Submissions">
              {summary.submissions.total === 0 ? (
                <span className="text-muted">—</span>
              ) : (
                <>
                  <span className="tabular-nums">
                    {summary.submissions.total}
                  </span>
                  <div className="mt-1 flex flex-wrap gap-x-2 font-mono text-[11px] font-normal">
                    {summary.submissions.running > 0 ? (
                      <span className="text-accent">
                        {summary.submissions.running} running
                      </span>
                    ) : null}
                    {summary.submissions.success > 0 ? (
                      <span className="text-muted">
                        {summary.submissions.success} done
                      </span>
                    ) : null}
                    {summary.submissions.failed > 0 ? (
                      <span className="text-error">
                        {summary.submissions.failed} failed
                      </span>
                    ) : null}
                  </div>
                </>
              )}
            </Stat>
            <Stat label="Versions">
              <span className="tabular-nums">{summary.version_count}</span>
            </Stat>
            <Stat label="Last activity">
              <span className="font-mono text-base font-normal text-muted">
                {formatTimestamp(summary.last_activity)}
              </span>
            </Stat>
          </section>

          <div className="mt-6 flex flex-wrap gap-5">
            {summary.is_live ? (
              <Link
                to={`/forms/${encodeURIComponent(formId!)}/form`}
                className="font-mono text-xs uppercase tracking-wider text-accent hover:text-accent-hover"
              >
                Start a submission →
              </Link>
            ) : null}
          </div>

          <div className="mt-10">
            <Tabs<FormTab>
              tabs={[
                { id: "overview", label: "Overview" },
                { id: "submissions", label: "Submissions" },
                { id: "theme", label: "Theme" },
                { id: "access", label: "Access" },
              ]}
              active={tab}
              onChange={setTab}
            />
            <div className="mt-8">
              {tab === "overview" ? (
                <WorkflowStructure formId={formId!} />
              ) : tab === "submissions" ? (
                <SubmissionsTab
                  formId={formId!}
                  onOpenSubmission={openSubmission}
                />
              ) : tab === "theme" ? (
                <ThemeTab formId={formId!} />
              ) : tab === "access" ? (
                <VisibilityControl formId={formId!} />
              ) : null}
            </div>
          </div>
        </>
      )}
      {formId ? (
        <SubmissionDrawer
          formId={formId}
          submissionId={openSubmissionId}
          onClose={closeSubmission}
        />
      ) : null}
    </main>
  );
}

/** The form's structural graph — content for the Overview tab. */
function WorkflowStructure({ formId }: { formId: string }) {
  const { data: graph, error, isLoading } = useFormGraph(formId);

  if (isLoading) {
    return <p className="text-muted text-sm">Loading workflow graph…</p>;
  }
  if (error) {
    return (
      <div className="border border-error bg-surface p-6">
        <p className="text-error text-sm">
          Couldn't load the workflow graph:{" "}
          {error instanceof ApiError ? error.message : "unknown error"}
        </p>
      </div>
    );
  }
  if (!graph) return null;
  return <WorkflowGraphCanvas graph={graph} />;
}

function Stat({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="flex-1 border-l border-border py-5 pl-5 first:border-l-0 first:pl-0">
      <div className="font-mono text-[11px] uppercase tracking-wider text-muted">
        {label}
      </div>
      <div className="mt-1.5 text-2xl text-ink">{children}</div>
    </div>
  );
}

