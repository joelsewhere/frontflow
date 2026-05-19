import { useEffect, useRef } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { useSubmission } from "../hooks/useSubmission";
import { DagChain } from "../components/dag/DagChain";
import { ProgressNode } from "../components/dag/ProgressNode";
import { HitlNode } from "../components/dag/HitlNode";
import { AirflowHitlNode } from "../components/dag/AirflowHitlNode";
import { BackendStepNode } from "../components/dag/BackendStepNode";
import { buildChainSegments } from "../lib/chainSegments";
import { buildSubmissionViews, type SubmissionView } from "../lib/submissionViews";

const enc = encodeURIComponent;

/**
 * Renders an in-flight or completed submission one **view** at a time.
 * The chain is split into views — each `@page`, plus flow views of
 * page-less top-level steps — and each view has its own URL:
 *
 *   /forms/:formId/form/submission/:submissionId/:viewId
 *
 * A single-view submission (a single-page form) stays at the bare
 * `/form/submission/:submissionId` URL. The bare URL of a multi-view
 * submission redirects to whichever view is currently live; as the
 * workflow advances, a user sitting on the live view is carried
 * forward into the next page. Browser back/forward and refresh work
 * because the view is in the URL.
 *
 * Also serves a session draft (`/form/draft`, addressed by a `handle`
 * in router state); when the draft's id is minted it moves to the
 * canonical `/form/submission/:submissionId` URL, keeping the view.
 */
export default function SubmissionPage() {
  const { formId, submissionId, viewId } = useParams<{
    formId: string;
    submissionId: string;
    viewId: string;
  }>();
  const location = useLocation();
  const navigate = useNavigate();

  // Draft mode: no `submissionId` in the URL — the submission is
  // addressed by a `handle` passed via router state (lost on refresh).
  const isDraft = !submissionId;
  const draftHandle = (location.state as { handle?: string } | null)?.handle;
  const key = submissionId ?? draftHandle;
  const navState = isDraft ? { handle: draftHandle } : undefined;

  const { data, error, isLoading } = useSubmission(formId, key);

  const views = data ? buildSubmissionViews(data.tasks) : [];
  const viewsSig = views.map((v) => v.viewId).join("|");
  const liveView: SubmissionView | undefined = views[views.length - 1];
  const isMulti = views.length > 1;

  // The view to render: a single-view submission renders at the bare
  // URL; a multi-view one is selected by the :viewId path segment.
  const currentView: SubmissionView | undefined = !isMulti
    ? views[0]
    : views.find((v) => v.viewId === viewId);
  const currentViewId = currentView?.viewId;

  const basePath = isDraft
    ? `/forms/${enc(formId ?? "")}/form/draft`
    : `/forms/${enc(formId ?? "")}/form/submission/${enc(submissionId ?? "")}`;
  const viewPath = (vId: string) => `${basePath}/${enc(vId)}`;

  // When a draft's id is minted, move to the canonical submission URL,
  // keeping the current view. `replace` so Back skips the dead draft.
  const mintedId = data?.submission_id ?? null;
  useEffect(() => {
    if (isDraft && mintedId && formId) {
      const suffix = viewId ? `/${enc(viewId)}` : "";
      navigate(
        `/forms/${enc(formId)}/form/submission/${enc(mintedId)}${suffix}`,
        { replace: true },
      );
    }
  }, [isDraft, mintedId, formId, viewId, navigate]);

  // Keep the URL pointing at a renderable view: a single-view
  // submission canonicalises to the bare URL; a multi-view one to a
  // valid :viewId, defaulting to the live view.
  useEffect(() => {
    if (!data || !formId || views.length === 0) return;
    if (views.length === 1) {
      if (viewId) navigate(basePath, { replace: true, state: navState });
      return;
    }
    const onValidView = viewId && views.some((v) => v.viewId === viewId);
    if (!onValidView && liveView) {
      navigate(viewPath(liveView.viewId), { replace: true, state: navState });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, viewsSig, viewId, formId]);

  // Auto-follow: when the workflow advances to a new live view and the
  // user is sitting on the previous live view, carry them forward.
  const prevLiveRef = useRef<string | null>(null);
  useEffect(() => {
    const liveId = liveView?.viewId ?? null;
    const prevLive = prevLiveRef.current;
    prevLiveRef.current = liveId;
    if (!isMulti || liveId === null || prevLive === null) return;
    if (prevLive !== liveId && currentViewId === prevLive) {
      navigate(viewPath(liveId), { state: navState });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveView?.viewId, currentViewId, isMulti]);

  // Scroll to the top of the view when the viewed page changes.
  const topRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    topRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [currentViewId]);

  const index = currentView
    ? views.findIndex((v) => v.viewId === currentView.viewId)
    : 0;

  const goToView = (vId: string) =>
    navigate(viewPath(vId), { state: navState });

  return (
    <main className="relative z-10 mx-auto max-w-7xl px-6 pt-12 pb-24">
      <div ref={topRef} />

      <header className="mb-8 flex flex-col gap-2">
        <Link
          to={formId ? `/forms/${enc(formId)}/form` : "/"}
          className="font-mono text-xs uppercase tracking-[0.25em] text-muted hover:text-ink transition-colors w-fit"
        >
          ← New submission
        </Link>
        {isDraft ? (
          <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-muted">
            Draft · not yet saved
          </p>
        ) : submissionId ? (
          <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-muted break-all">
            {submissionId}
          </p>
        ) : null}
      </header>

      {isDraft && !draftHandle && (
        <div className="border border-border bg-surface p-6">
          <p className="text-sm text-ink">This draft is no longer available.</p>
          <p className="mt-2 text-sm text-muted">
            A submission isn't saved until it's been given an id — start
            again from the form.
          </p>
        </div>
      )}

      {key && isLoading && <p className="text-muted">Loading status…</p>}

      {error && (
        <div className="border border-error bg-surface p-6">
          <p className="text-error text-sm">{error.message}</p>
        </div>
      )}

      {data && formId && key && currentView && (
        <>
          {isMulti && (
            <PageNav
              views={views}
              index={index}
              onBack={() => {
                if (index > 0) goToView(views[index - 1].viewId);
              }}
              onForward={() => {
                if (index < views.length - 1)
                  goToView(views[index + 1].viewId);
              }}
            />
          )}

          {/* The form itself stays a comfortable reading column,
              centered within the full-width page. */}
          <div className="mx-auto w-full max-w-2xl">
            {currentView.kind === "page" && (
              <div className="mb-6">
                <p className="font-mono text-[10px] uppercase tracking-[0.3em] text-muted mb-1.5">
                  Page
                </p>
                <h2 className="font-display text-2xl font-bold leading-tight text-ink">
                  {currentView.pageTitle ?? currentView.pageId}
                </h2>
              </div>
            )}

            <ViewChain
              key={currentView.key}
              view={currentView}
              formId={formId}
              submissionId={key}
            />
          </div>
        </>
      )}
    </main>
  );
}

/** The back/forward strip shown when a submission spans multiple views. */
function PageNav({
  views,
  index,
  onBack,
  onForward,
}: {
  views: SubmissionView[];
  index: number;
  onBack: () => void;
  onForward: () => void;
}) {
  const view = views[index];
  const label =
    view.kind === "page" ? (view.pageTitle ?? view.pageId ?? "Page") : "Steps";

  const btn =
    "border border-border px-3 py-2 font-mono text-[10px] uppercase " +
    "tracking-[0.2em] transition-colors disabled:opacity-25 " +
    "disabled:cursor-not-allowed enabled:hover:text-ink " +
    "enabled:hover:border-ink text-muted";

  return (
    <div className="mb-6 flex items-center justify-between gap-3">
      <button
        type="button"
        className={btn}
        onClick={onBack}
        disabled={index === 0}
      >
        ← Back
      </button>
      <span className="font-mono text-[10px] uppercase tracking-[0.2em] text-muted text-center truncate">
        {label}
        <span className="text-border"> · </span>
        {index + 1} / {views.length}
      </span>
      <button
        type="button"
        className={btn}
        onClick={onForward}
        disabled={index >= views.length - 1}
      >
        Next →
      </button>
    </div>
  );
}

/** Renders one view's tasks as a scoped DAG chain. */
function ViewChain({
  view,
  formId,
  submissionId,
}: {
  view: SubmissionView;
  formId: string;
  submissionId: string;
}) {
  const segments = buildChainSegments(view.tasks);

  return (
    <DagChain>
      {segments.map((seg, i) => {
        const stepLabel = `STEP ${String(i + 1).padStart(2, "0")}`;
        if (seg.kind === "tasks") {
          return (
            <ProgressNode
              key={`progress-${seg.tasks[0].task_id}`}
              tasks={seg.tasks}
              formId={formId}
              submissionId={submissionId}
              stepLabel={stepLabel}
            />
          );
        }
        if (seg.kind === "backend") {
          return (
            <BackendStepNode
              key={`backend-${seg.task.task_id}`}
              taskId={seg.task.task_id}
              state={seg.task.state}
              stepLabel={stepLabel}
            />
          );
        }
        if (seg.kind === "airflowHitl") {
          return (
            <AirflowHitlNode
              key={`airflow-hitl-${seg.task.task_id}`}
              task={seg.task}
              formId={formId}
              submissionId={submissionId}
              stepLabel={stepLabel}
            />
          );
        }
        return (
          <HitlNode
            key={`hitl-${seg.task.task_id}`}
            formId={formId}
            submissionId={submissionId}
            stepId={seg.task.task_id}
            stepLabel={stepLabel}
            cascadeStatus={seg.task.status}
          />
        );
      })}
    </DagChain>
  );
}
