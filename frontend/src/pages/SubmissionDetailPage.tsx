import { useCallback, useMemo, useRef, useState } from "react";
import {
  Link,
  Navigate,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { useSubmissionDetail } from "../hooks/useSubmissionDetail";
import { useFormVersionSource } from "../hooks/useFormSource";
import { useAuth } from "../auth/AuthContext";
import { StatePill } from "../components/listing/StatePill";
import { SubmissionGraph } from "../components/submission/SubmissionGraph";
import { PythonSource } from "../components/source/PythonSource";
import {
  EventLine,
  RepinControl,
  StepBlock,
  VersionPicker,
} from "../components/submission/SubmissionSummaryContent";
import { CompareVersionsModal } from "../components/submission/CompareVersionsModal";
import { formatTimestamp } from "../lib/format";
import { formatVersion, compareVersion } from "../lib/version";
import { ApiError, type StepDetailRow, type SubmissionDetail } from "../lib/api";

/**
 * Dedicated per-submission page at /forms/:formId/submissions/:submissionId.
 *
 * Layout (Airflow-style run detail):
 *   ┌────────────────────────────────────────────────────────────┐
 *   │ Header — state, timing, version picker, error, controls    │
 *   ├──────────────────────────────────┬─────────────────────────┤
 *   │ Main: Graph | List (tab strip)   │ Drawer (always open)    │
 *   │                                  │ Overview | Step | Hist  │
 *   │                                  │                         │
 *   │                                  │                         │
 *   └──────────────────────────────────┴─────────────────────────┘
 *
 * Graph is the primary navigation: clicking a node selects the
 * step (URL `?step=<node_id>`), switches the drawer to its Step
 * tab, and highlights the node. List preserves the flat-list view
 * for users who want to scan everything.
 *
 * URL params, all default-omitted:
 *   ?view=list                — switch main panel to list (default graph)
 *   ?step=<node_id>           — selected step
 *   ?tab=step|history         — drawer tab (default overview)
 */
export default function SubmissionDetailPage() {
  const { formId, submissionId } = useParams<{
    formId: string;
    submissionId: string;
  }>();
  if (!formId || !submissionId) {
    return <Navigate to="/forms" replace />;
  }
  return <Inner formId={formId} submissionId={submissionId} />;
}

function Inner({
  formId,
  submissionId,
}: {
  formId: string;
  submissionId: string;
}) {
  const [pickedVersionId, setPickedVersionId] = useState<number | undefined>(
    undefined,
  );
  const [compareOpen, setCompareOpen] = useState(false);
  const { data: detail, error, isLoading, refetch } = useSubmissionDetail(
    formId,
    submissionId,
    pickedVersionId,
  );
  const { user } = useAuth();

  // URL-backed page state.
  const [searchParams, setSearchParams] = useSearchParams();
  const view: "graph" | "list" =
    searchParams.get("view") === "list" ? "list" : "graph";
  const selectedStep = searchParams.get("step");
  const drawerTab: "overview" | "step" | "history" | "source" = (() => {
    const t = searchParams.get("tab");
    if (t === "step" || t === "history" || t === "source") return t;
    return "overview";
  })();
  const graphOrientation: "LR" | "TB" =
    searchParams.get("graph_orientation") === "TB" ? "TB" : "LR";

  const updateParams = useCallback(
    (mutate: (p: URLSearchParams) => void) => {
      const next = new URLSearchParams(searchParams);
      mutate(next);
      setSearchParams(next, { replace: false });
    },
    [searchParams, setSearchParams],
  );
  const setView = useCallback(
    (v: "graph" | "list") =>
      updateParams((p) => {
        if (v === "graph") p.delete("view");
        else p.set("view", "list");
      }),
    [updateParams],
  );
  const setGraphOrientation = useCallback(
    (o: "LR" | "TB") =>
      updateParams((p) => {
        if (o === "LR") p.delete("graph_orientation");
        else p.set("graph_orientation", "TB");
      }),
    [updateParams],
  );
  const setSelectedStep = useCallback(
    (nodeId: string | null, opts: { focusDrawer?: boolean } = {}) =>
      updateParams((p) => {
        if (nodeId === null) p.delete("step");
        else p.set("step", nodeId);
        if (opts.focusDrawer) p.set("tab", "step");
      }),
    [updateParams],
  );
  const setDrawerTab = useCallback(
    (t: "overview" | "step" | "history" | "source") =>
      updateParams((p) => {
        if (t === "overview") p.delete("tab");
        else p.set("tab", t);
      }),
    [updateParams],
  );

  // Scroll-to-step refs for the List view — same pattern as before.
  const stepBlockRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  const flashStep = selectedStep; // visual flash uses selection state directly

  // Resizable drawer width.
  const { width: drawerWidth, setWidth: setDrawerWidth, commit: commitDrawerWidth } =
    useDrawerWidth();
  const dragStateRef = useRef<{ startX: number; startWidth: number } | null>(
    null,
  );
  const onDividerPointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      e.preventDefault();
      // capture so we keep receiving move events even if the cursor
      // leaves the handle's hitbox during a fast drag.
      (e.currentTarget as HTMLDivElement).setPointerCapture(e.pointerId);
      dragStateRef.current = { startX: e.clientX, startWidth: drawerWidth };
    },
    [drawerWidth],
  );
  const onDividerPointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const s = dragStateRef.current;
      if (!s) return;
      // Drag the divider LEFT (clientX decreases) → drawer grows.
      // Right → drawer shrinks. The drag handle is at the boundary
      // between the main panel and the drawer; moving it left makes
      // the drawer wider.
      const delta = e.clientX - s.startX;
      setDrawerWidth(s.startWidth - delta);
    },
    [setDrawerWidth],
  );
  const onDividerPointerUp = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (dragStateRef.current) {
        dragStateRef.current = null;
        try {
          (e.currentTarget as HTMLDivElement).releasePointerCapture(
            e.pointerId,
          );
        } catch {
          // pointer already released — ignore.
        }
        commitDrawerWidth();
      }
    },
    [commitDrawerWidth],
  );

  const navigate = useNavigate();

  const handleNodeClick = useCallback(
    (nodeId: string) => {
      setSelectedStep(nodeId, { focusDrawer: true });
      // If in List view, also scroll the block into view.
      if (view === "list") {
        window.setTimeout(() => {
          const el = stepBlockRefs.current.get(nodeId);
          if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
        }, 0);
      }
    },
    [setSelectedStep, view],
  );

  // Click on a node inside a nested child cluster (or on the cluster
  // itself) — navigate to the child submission's detail page, focused
  // on the step that was clicked when applicable. Submission id is
  // preferred over handle for shareability; we fall back to the
  // handle when the child hasn't been assigned an id yet.
  const handleChildNodeClick = useCallback(
    ({
      childFormId,
      childHandle,
      childSubmissionId,
      childStepId,
    }: {
      childFormId: string;
      childHandle: string;
      childSubmissionId: string | null;
      childStepId: string;
    }) => {
      const subId = childSubmissionId ?? childHandle;
      const base = `/forms/${encodeURIComponent(childFormId)}/submissions/${encodeURIComponent(subId)}`;
      // Land on the step inside the child if we have one; otherwise
      // open the submission's default tab.
      const url = childStepId
        ? `${base}?step=${encodeURIComponent(childStepId)}&tab=step`
        : base;
      navigate(url);
    },
    [navigate],
  );

  if (isLoading) {
    return (
      <main className="relative z-10 mx-auto max-w-7xl px-6 pt-24 pb-16">
        <p className="font-display text-2xl text-ink opacity-30">Loading…</p>
      </main>
    );
  }
  if (error) {
    return (
      <main className="relative z-10 mx-auto max-w-7xl px-6 pt-24 pb-16">
        <div className="border border-error bg-surface p-6">
          <p className="text-error text-sm">
            Couldn't load the submission:{" "}
            {error instanceof Error ? error.message : "unknown error"}
          </p>
        </div>
      </main>
    );
  }
  if (!detail) return null;

  // Repin is offered whenever the live (major, minor) is strictly
  // greater than the submission's pinned (major, minor) — minor-only
  // deltas count too, since the runtime behavior may have changed
  // (helper-function edits, for example). The compareVersion helper
  // owns the tuple-comparison rule.
  const canRepin =
    !!user?.is_admin &&
    detail.is_viewing_active &&
    compareVersion(
      {
        major: detail.live_form_version,
        minor: detail.live_minor_version,
      },
      {
        major: detail.form_version,
        minor: detail.form_minor_version,
      },
    ) > 0;
  const hasMultipleVersions = detail.available_versions.length > 1;

  return (
    <main className="relative z-10 mx-auto max-w-[1600px] px-6 pt-12 pb-16">
      {/* Breadcrumb */}
      <Link
        to={`/forms/${encodeURIComponent(formId)}?tab=submissions`}
        className="font-mono text-xs uppercase tracking-wider text-muted hover:text-accent"
      >
        ← Back to submissions
      </Link>

      {/* Header */}
      <header className="mt-6 mb-6 flex flex-wrap items-start justify-between gap-4">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-3">
            <StatePill state={detail.state} />
            <h1 className="font-display text-2xl text-ink truncate">
              {detail.submission_id ?? detail.handle}
            </h1>
          </div>
          <p className="mt-2 font-mono text-xs text-muted">
            Started {formatTimestamp(detail.created_at)}
            {detail.terminated_at ? (
              <> · Ended {formatTimestamp(detail.terminated_at)}</>
            ) : null}
            <> · </>
            <span className="text-ink">
              {formatVersion(detail.form_version, detail.form_minor_version)}
            </span>
            {/*
              Version-context suffix. Without this, a bare `v1`
              gives no signal about whether the user is on the
              latest, behind, or viewing a frozen historical chain.
              Comparison uses the (major, minor) tuple so a minor-
              only delta (v1 → v1.1) is also surfaced as "available".
            */}
            {!detail.is_viewing_active ? (
              <span className="ml-1.5 text-muted">
                (viewing frozen{" "}
                {formatVersion(
                  detail.viewing_version,
                  detail.viewing_minor_version,
                )}
                {" — active is "}
                {formatVersion(
                  detail.form_version,
                  detail.form_minor_version,
                )}
                )
              </span>
            ) : compareVersion(
                {
                  major: detail.live_form_version,
                  minor: detail.live_minor_version,
                },
                {
                  major: detail.form_version,
                  minor: detail.form_minor_version,
                },
              ) > 0 ? (
              <span className="ml-1.5 text-accent">
                ({formatVersion(
                  detail.live_form_version,
                  detail.live_minor_version,
                )}{" available"})
              </span>
            ) : (
              <span className="ml-1.5 text-muted">(latest)</span>
            )}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          {canRepin ? (
            <RepinControl
              formId={formId}
              submissionId={submissionId}
              fromVersion={detail.form_version}
              toVersion={detail.live_form_version}
              fromMinorVersion={detail.form_minor_version}
              toMinorVersion={detail.live_minor_version}
              onRepinned={() => refetch()}
            />
          ) : null}
          {hasMultipleVersions ? (
            <VersionPicker
              options={detail.available_versions}
              selected={detail.viewing_version_id}
              onSelect={(id, isActive) =>
                setPickedVersionId(isActive ? undefined : id)
              }
              onCompare={
                user?.is_admin ? () => setCompareOpen(true) : undefined
              }
            />
          ) : null}
          {hasMultipleVersions && user?.is_admin ? (
            <CompareVersionsModal
              open={compareOpen}
              onClose={() => setCompareOpen(false)}
              formId={formId}
              versions={detail.available_versions}
              initialToId={detail.viewing_version_id}
              initialFromId={(() => {
                // Default FROM = the entry one step before the
                // currently-viewed version (oldest-first list).
                // Falls back to the same id when there's no prior;
                // the modal then renders "(identical)" until the
                // user picks a different FROM.
                const opts = detail.available_versions;
                const idx = opts.findIndex(
                  (o) => o.id === detail.viewing_version_id,
                );
                return idx > 0
                  ? opts[idx - 1].id
                  : detail.viewing_version_id;
              })()}
            />
          ) : null}
          <a
            href={`/forms/${encodeURIComponent(formId)}/form/submission/${encodeURIComponent(
              detail.submission_id ?? detail.handle,
            )}`}
            className="font-mono text-xs uppercase tracking-wider text-accent hover:text-accent-hover"
          >
            View the run →
          </a>
        </div>
      </header>

      {!detail.is_viewing_active ? (
        <div className="mb-6 border border-muted bg-surface-muted p-4">
          <p className="font-mono text-[11px] uppercase tracking-wider text-muted">
            Read-only history
          </p>
          <p className="mt-1 text-sm text-ink">
            Viewing the frozen v{detail.viewing_version} chain. This data
            cannot be edited or rerun; switch back to v{detail.form_version}{" "}
            (active) to interact with the submission.
          </p>
        </div>
      ) : null}

      {detail.error ? (
        <div className="mb-6 border border-error bg-surface p-5">
          <p className="font-mono text-[11px] uppercase tracking-wider text-error">
            Failed
          </p>
          <p className="mt-1 text-sm text-ink">{detail.error}</p>
        </div>
      ) : null}

      {/* Main + Drawer split. Two flex columns: main grows, drawer
          has a fixed-for-M1 width. M2 will add a draggable divider. */}
      <div className="flex gap-4 items-stretch">
        {/* Main panel */}
        <div className="flex-1 min-w-0 flex flex-col">
          {/* View tab strip — Graph (default) | List | Source */}
          <div className="mb-4 flex items-end gap-1 border-b border-border">
            {(["graph", "list"] as const).map((v) => {
              const isActive = view === v;
              return (
                <button
                  key={v}
                  type="button"
                  onClick={() => setView(v)}
                  className={[
                    "font-mono text-xs uppercase tracking-[0.16em] px-3 py-2 border-b -mb-px",
                    isActive
                      ? "border-ink text-ink"
                      : "border-transparent text-muted hover:text-ink",
                  ].join(" ")}
                >
                  {v === "graph" ? "Graph" : "List"}
                </button>
              );
            })}
          </div>
          {view === "graph" ? (
            <div
              className="border border-border bg-surface"
              style={{ height: 620 }}
            >
              <SubmissionGraph
                formId={formId}
                steps={detail.steps}
                onNodeClick={handleNodeClick}
                childGraphs={detail.child_graphs}
                onChildNodeClick={handleChildNodeClick}
                orientation={graphOrientation}
                onOrientationChange={setGraphOrientation}
              />
            </div>
          ) : detail.steps.length === 0 ? (
            <p className="text-muted text-sm">No steps recorded.</p>
          ) : (
            <div className="flex flex-col gap-4">
              {detail.steps.map((step) => (
                <div
                  key={step.seq}
                  ref={(el) => {
                    if (el) stepBlockRefs.current.set(step.node_id, el);
                    else stepBlockRefs.current.delete(step.node_id);
                  }}
                  className={
                    flashStep === step.node_id
                      ? "transition-shadow duration-300 ring-2 ring-accent ring-offset-2 ring-offset-bg cursor-pointer"
                      : "transition-shadow duration-300 cursor-pointer"
                  }
                  onClick={() =>
                    setSelectedStep(step.node_id, { focusDrawer: true })
                  }
                >
                  <StepBlock step={step} />
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Drag handle — sits between main panel and drawer.
            Pointer-capture model: drag starts on pointerdown,
            tracks regardless of where the cursor goes, releases
            on pointerup. Width persists to localStorage on release
            so the user's preferred width survives across pages. */}
        <div
          role="separator"
          aria-orientation="vertical"
          onPointerDown={onDividerPointerDown}
          onPointerMove={onDividerPointerMove}
          onPointerUp={onDividerPointerUp}
          className="w-1 cursor-col-resize bg-border hover:bg-accent active:bg-accent transition-colors"
          style={{ touchAction: "none" }}
          title="Drag to resize"
        />

        {/* Drawer — always open, draggable-resizable width. */}
        <aside
          className="border border-border bg-surface flex flex-col"
          style={{ width: drawerWidth, minWidth: drawerWidth, flexShrink: 0 }}
        >
          {/* Drawer tab strip */}
          <div className="flex border-b border-border">
            {(["overview", "step", "history", "source"] as const).map((t) => {
              const isActive = drawerTab === t;
              return (
                <button
                  key={t}
                  type="button"
                  onClick={() => setDrawerTab(t)}
                  className={[
                    "flex-1 font-mono text-xs uppercase tracking-[0.16em] px-3 py-3 border-b -mb-px",
                    isActive
                      ? "border-ink text-ink bg-bg"
                      : "border-transparent text-muted hover:text-ink",
                  ].join(" ")}
                >
                  {t === "overview"
                    ? "Overview"
                    : t === "step"
                      ? "Step"
                      : t === "history"
                        ? "History"
                        : "Source"}
                </button>
              );
            })}
          </div>
          <div className="flex-1 overflow-y-auto p-4">
            {drawerTab === "overview" ? (
              <OverviewTab detail={detail} />
            ) : drawerTab === "step" ? (
              <StepTab
                selectedNodeId={selectedStep}
                steps={detail.steps}
              />
            ) : drawerTab === "history" ? (
              <HistoryTab events={detail.events} />
            ) : (
              <SubmissionSourceView
                formId={formId}
                version={detail.form_version}
              />
            )}
          </div>
        </aside>
      </div>
    </main>
  );
}

// --- Drawer tab content ----------------------------------------------------

/** Submission-level summary. v1: stats only; M2 adds a step-timeline
 *  visualization. The drawer is narrow so layout is single-column. */
function OverviewTab({ detail }: { detail: SubmissionDetail }) {
  // Counts by state across the submission's step list.
  const counts = useMemo(() => {
    const out = { submitted: 0, awaiting: 0, failed: 0 };
    for (const s of detail.steps) {
      if (s.state === "submitted") out.submitted += 1;
      else if (s.state === "awaiting") out.awaiting += 1;
      else if (s.state === "failed") out.failed += 1;
    }
    return out;
  }, [detail.steps]);

  const currentStep = detail.steps.find((s) => s.state === "awaiting");
  const elapsedMs = detail.terminated_at
    ? new Date(detail.terminated_at).getTime() -
      new Date(detail.created_at).getTime()
    : null;

  return (
    <div className="flex flex-col gap-5">
      <Field label="State">
        <StatePill state={detail.state} />
      </Field>
      <Field label="Submission ID">
        <span className="font-mono text-xs text-ink break-all">
          {detail.submission_id ?? detail.handle}
        </span>
      </Field>
      <Field label="Form version">
        v{detail.form_version}
        {detail.live_form_version > detail.form_version ? (
          <span className="ml-1 text-muted text-xs">
            (latest: v{detail.live_form_version})
          </span>
        ) : null}
      </Field>
      <Field label="Started">{formatTimestamp(detail.created_at)}</Field>
      {detail.terminated_at ? (
        <Field label="Ended">{formatTimestamp(detail.terminated_at)}</Field>
      ) : null}
      {elapsedMs !== null ? (
        <Field label="Duration">{formatDurationMs(elapsedMs)}</Field>
      ) : null}
      {currentStep ? (
        <Field label="Current step">
          <span className="font-mono text-xs text-ink">
            {currentStep.title ?? currentStep.node_id}
          </span>
        </Field>
      ) : null}
      <Field label="Steps">
        <div className="flex gap-3 text-xs">
          <span>
            <span className="text-ink">{counts.submitted}</span>{" "}
            <span className="text-muted">done</span>
          </span>
          {counts.awaiting > 0 ? (
            <span>
              <span className="text-ink">{counts.awaiting}</span>{" "}
              <span className="text-muted">active</span>
            </span>
          ) : null}
          {counts.failed > 0 ? (
            <span>
              <span className="text-error">{counts.failed}</span>{" "}
              <span className="text-muted">failed</span>
            </span>
          ) : null}
        </div>
      </Field>
      {detail.error ? (
        <Field label="Error">
          <p className="text-error text-xs">{detail.error}</p>
        </Field>
      ) : null}
      <StepTimeline detail={detail} />
    </div>
  );
}

/** Per-step duration bars for this submission. Each step gets a
 *  row with its label and a horizontal bar sized relative to the
 *  longest step in this submission (NOT relative to other
 *  submissions' steps — this is a per-submission view, not a
 *  comparative one). Header shows both the sum of step durations
 *  and the wall-clock total; the difference is "between-step time"
 *  (the submission existed but wasn't actively in any step), which
 *  matters for HITL forms where users walk away. */
function StepTimeline({
  detail,
}: {
  detail: SubmissionDetail;
}) {
  // For each step, compute its duration: ended-at - started-at if
  // submitted, or now - started-at if still active. Failed steps
  // count up to the failure point. Steps without started_at (rare;
  // legacy data) get skipped so the chart doesn't show empty rows.
  const rows = useMemo(() => {
    const now = Date.now();
    return detail.steps
      .filter((s) => !!s.started_at)
      .map((s) => {
        const start = new Date(s.started_at!).getTime();
        const end = s.submitted_at
          ? new Date(s.submitted_at).getTime()
          : s.state === "awaiting"
            ? now
            : start; // failed without submitted_at — degenerate
        const durMs = Math.max(0, end - start);
        return {
          nodeId: s.node_id,
          label: s.title ?? s.node_id,
          state: s.state,
          durMs,
        };
      });
  }, [detail.steps]);

  if (rows.length === 0) return null;

  const maxDur = Math.max(1, ...rows.map((r) => r.durMs));

  return (
    <div>
      <p className="mb-3 font-mono text-[10px] uppercase tracking-[0.16em] text-muted">
        Step durations
      </p>
      <div className="flex flex-col gap-1.5">
        {rows.map((r) => {
          const pct = (r.durMs / maxDur) * 100;
          // Bar color follows the step's state so the timeline
          // doubles as a state map alongside the duration encoding.
          const barColor =
            r.state === "failed"
              ? "bg-error"
              : r.state === "awaiting"
                ? "bg-accent"
                : "bg-ink";
          return (
            <div key={r.nodeId} className="flex items-center gap-2 text-xs">
              <span
                className="font-mono text-[11px] text-ink truncate"
                style={{ minWidth: 90, maxWidth: 90 }}
                title={r.label}
              >
                {r.label}
              </span>
              <div className="relative flex-1 h-3 bg-surface border border-border">
                <div
                  className={`h-full ${barColor}`}
                  style={{ width: `${pct}%` }}
                />
              </div>
              <span
                className="font-mono text-[10px] text-muted tabular-nums text-right"
                style={{ minWidth: 44 }}
              >
                {formatDurationMs(r.durMs)}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** Contextual: shows the selected step's detail block. When nothing
 *  is selected, prompts the user to pick a step from the graph or
 *  list — the drawer remains useful (Overview/History) but this tab
 *  needs a selection to populate. */
function StepTab({
  selectedNodeId,
  steps,
}: {
  selectedNodeId: string | null;
  steps: StepDetailRow[];
}) {
  if (!selectedNodeId) {
    return (
      <p className="font-mono text-xs text-muted">
        Click a step in the graph (or list) to view its details here.
      </p>
    );
  }
  // The same node may have multiple step rows (loops / re-entries
  // aren't a thing today, but edits create multiple submission
  // entries for a step). Show the most recent.
  const matches = steps.filter((s) => s.node_id === selectedNodeId);
  const step = matches.length > 0 ? matches[matches.length - 1] : null;
  if (!step) {
    return (
      <p className="font-mono text-xs text-muted">
        This step hasn't been reached by this submission yet.
      </p>
    );
  }
  return <StepBlock step={step} />;
}

function HistoryTab({
  events,
}: {
  events: Parameters<typeof EventLine>[0]["event"][];
}) {
  if (events.length === 0) {
    return <p className="font-mono text-xs text-muted">No events.</p>;
  }
  return (
    <ol>
      {events.map((event, i) => (
        <EventLine key={i} event={event} />
      ))}
    </ol>
  );
}

// --- Helpers ---------------------------------------------------------------

/** Width range for the resizable drawer. Below ~280 the tab labels
 *  collide and the Overview rows wrap unreadably; above ~700 the
 *  graph canvas gets too cramped on typical screens. */
const DRAWER_WIDTH_MIN = 280;
const DRAWER_WIDTH_MAX = 700;
const DRAWER_WIDTH_DEFAULT = 420;
const DRAWER_WIDTH_KEY = "submission-detail-drawer-width";

/** Resizable-drawer state. Hydrates from localStorage on mount;
 *  persists on release rather than on every move (avoids spamming
 *  localStorage during drag). Returns the current width, a setter
 *  for the live drag, and a "commit" function the drag handler
 *  calls when the user releases. */
function useDrawerWidth(): {
  width: number;
  setWidth: (w: number) => void;
  commit: () => void;
} {
  const [width, setWidthRaw] = useState<number>(() => {
    try {
      const raw = localStorage.getItem(DRAWER_WIDTH_KEY);
      if (raw) {
        const n = parseInt(raw, 10);
        if (
          Number.isFinite(n) &&
          n >= DRAWER_WIDTH_MIN &&
          n <= DRAWER_WIDTH_MAX
        ) {
          return n;
        }
      }
    } catch {
      // localStorage can throw in private browsing — fall through.
    }
    return DRAWER_WIDTH_DEFAULT;
  });
  const setWidth = useCallback((w: number) => {
    const clamped = Math.max(
      DRAWER_WIDTH_MIN,
      Math.min(DRAWER_WIDTH_MAX, w),
    );
    setWidthRaw(clamped);
  }, []);
  const commit = useCallback(() => {
    try {
      localStorage.setItem(DRAWER_WIDTH_KEY, String(width));
    } catch {
      // Same — ignore quota / access errors silently.
    }
  }, [width]);
  return { width, setWidth, commit };
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-muted mb-1">
        {label}
      </p>
      <div className="text-sm text-ink">{children}</div>
    </div>
  );
}

function formatDurationMs(ms: number): string {
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  const d = Math.floor(h / 24);
  return `${d}d ${h % 24}h`;
}

/**
 * Read-only source view for a submission's pinned form_version.
 * Shows the exact Python code the submission was running, even if
 * the live form has since been edited and bumped past that version
 * — important for investigating an old submission's behavior.
 *
 * Admin-only (the backend enforces this); non-admins see a friendly
 * 403 message. Cached forever within the session since a pinned
 * version's source is immutable.
 */
function SubmissionSourceView({
  formId,
  version,
}: {
  formId: string;
  version: number;
}) {
  const { data, error, isLoading } = useFormVersionSource(formId, version);

  if (isLoading) {
    return <div className="font-mono text-sm text-muted">Loading source…</div>;
  }
  if (error) {
    const apiError = error instanceof ApiError ? error : null;
    if (apiError?.status === 403) {
      return (
        <div className="text-sm text-muted">
          Viewing the form source requires admin access.
        </div>
      );
    }
    return (
      <div className="text-sm text-danger">
        Couldn't load source: {(error as Error).message}
      </div>
    );
  }
  if (!data) return null;

  return (
    <div>
      <div className="mb-3 flex items-center gap-3 text-xs text-muted">
        <span className="font-mono uppercase tracking-wider">
          form_version
        </span>
        <span className="tabular-nums text-ink">{data.version}</span>
      </div>
      <PythonSource source={data.source} />
    </div>
  );
}
