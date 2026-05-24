import { useCallback, type ReactNode } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ApiError } from "../lib/api";
import { formatTimestamp } from "../lib/format";
import { useFormDetail } from "../hooks/useFormDetail";
import { useFormGraph } from "../hooks/useFormGraph";
import { useFormsList } from "../hooks/useFormsList";
import { useFormTab, type FormTab } from "../hooks/useFormTab";
import { ReparseButton } from "../components/listing/ReparseButton";
import { WorkflowGraphCanvas } from "../components/graph/WorkflowGraphCanvas";
import { VisibilityControl } from "../components/console/VisibilityControl";
import { SubmissionsTab } from "../components/form/SubmissionsTab";
import { ReportsTab } from "../components/form/ReportsTab";
import { ThemeTab } from "../components/form/ThemeTab";
import { Tabs } from "../components/ui/Tabs";

/**
 * Form summary (`/forms/:formId`) — a form's console home: identity,
 * description, submission/version stats, and the primary actions, with
 * the rest of the form's surface organized into tabs:
 *   - Overview: the workflow graph (default)
 *   - Submissions: paginated full list; row click navigates to the
 *     dedicated per-submission page
 *   - Theme: the form's end-user styling editor
 *   - Access: visibility / permission grants
 *
 * The active tab is in `?tab=`; deep links and bookmarks survive.
 */
export default function FormSummaryPage() {
  const { formId } = useParams<{ formId: string }>();
  const { data: forms, error: formsError, isLoading } = useFormsList();
  const { data: detail } = useFormDetail(formId);
  const navigate = useNavigate();
  const [tab, setTab] = useFormTab();
  const [searchParams, setSearchParams] = useSearchParams();

  // Switch to the Submissions tab, optionally pre-filtered by state.
  // Goes through the URL directly (not useFormTab's setTab) so the
  // `tab` and `state` params can be set in one navigation — useFormTab
  // builds from the current params but doesn't know about `state`.
  const goToSubmissions = useCallback(
    (state?: "running" | "success" | "failed") => {
      const next = new URLSearchParams(searchParams);
      next.set("tab", "submissions");
      if (state) next.set("state", state);
      else next.delete("state");
      setSearchParams(next, { replace: false });
    },
    [searchParams, setSearchParams],
  );

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
                  <button
                    type="button"
                    onClick={() => goToSubmissions()}
                    className="tabular-nums text-left hover:text-accent transition-colors"
                  >
                    {summary.submissions.total}
                  </button>
                  <div className="mt-1 flex flex-wrap gap-x-2 font-mono text-[11px] font-normal">
                    {summary.submissions.running > 0 ? (
                      <button
                        type="button"
                        onClick={() => goToSubmissions("running")}
                        className="text-accent hover:underline"
                      >
                        {summary.submissions.running} running
                      </button>
                    ) : null}
                    {summary.submissions.success > 0 ? (
                      <button
                        type="button"
                        onClick={() => goToSubmissions("success")}
                        className="text-muted hover:text-ink hover:underline"
                      >
                        {summary.submissions.success} done
                      </button>
                    ) : null}
                    {summary.submissions.failed > 0 ? (
                      <button
                        type="button"
                        onClick={() => goToSubmissions("failed")}
                        className="text-error hover:underline"
                      >
                        {summary.submissions.failed} failed
                      </button>
                    ) : null}
                  </div>
                </>
              )}
            </Stat>
            <Stat label="Versions">
              <span className="tabular-nums">{summary.version_count}</span>
            </Stat>
            <Stat label="Last activity">
              {summary.last_activity_submission_id ? (
                <button
                  type="button"
                  onClick={() =>
                    navigate(
                      `/forms/${encodeURIComponent(formId!)}/submissions/${encodeURIComponent(
                        summary.last_activity_submission_id!,
                      )}`,
                    )
                  }
                  className={[
                    "font-mono text-base font-normal text-left transition-colors hover:underline",
                    // Color the timestamp by the source submission's
                    // current state. "Last activity" is a glanceable
                    // health signal — a long-quiet form lighting up
                    // red (failed) reads very differently from a
                    // green (success) or accent (running) update.
                    summary.last_activity_state === "failed"
                      ? "text-error"
                      : summary.last_activity_state === "running"
                        ? "text-accent"
                        : "text-muted hover:text-ink",
                  ].join(" ")}
                >
                  {formatTimestamp(summary.last_activity)}
                </button>
              ) : (
                <span className="font-mono text-base font-normal text-muted">
                  {formatTimestamp(summary.last_activity)}
                </span>
              )}
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
                { id: "reports", label: "Reports" },
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
                  onOpenSubmission={(sid) =>
                    navigate(
                      `/forms/${encodeURIComponent(formId!)}/submissions/${encodeURIComponent(sid)}`,
                    )
                  }
                />
              ) : tab === "reports" ? (
                <ReportsTab formId={formId!} />
              ) : tab === "theme" ? (
                <ThemeTab formId={formId!} />
              ) : tab === "access" ? (
                <VisibilityControl formId={formId!} />
              ) : null}
            </div>
          </div>
        </>
      )}
    </main>
  );
}

/** The form's structural graph — content for the Overview tab. */
function WorkflowStructure({ formId }: { formId: string }) {
  const { data: graph, error, isLoading } = useFormGraph(formId);
  // URL-backed orientation — same param name as the per-submission
  // page so the toggle's URL semantics are consistent across both
  // graph views in the console.
  const [searchParams, setSearchParams] = useSearchParams();
  const orientation: "LR" | "TB" =
    searchParams.get("graph_orientation") === "TB" ? "TB" : "LR";
  const setOrientation = (o: "LR" | "TB") => {
    const next = new URLSearchParams(searchParams);
    if (o === "LR") next.delete("graph_orientation");
    else next.set("graph_orientation", "TB");
    setSearchParams(next, { replace: false });
  };

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
  return (
    <WorkflowGraphCanvas
      graph={graph}
      orientation={orientation}
      onOrientationChange={setOrientation}
    />
  );
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

