import { Handle, Position, type NodeProps } from "@xyflow/react";

export type Orientation = "LR" | "TB";

/** Handle sides for the orientation — flow enters one side, leaves the
 *  other (mid-left/right for LR, top/bottom-centre for TB). */
function handleSides(o: Orientation) {
  return o === "LR"
    ? { target: Position.Left, source: Position.Right }
    : { target: Position.Top, source: Position.Bottom };
}

function humanize(id: string): string {
  const s = id.replace(/_/g, " ").trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function Tag({ children }: { children: string }) {
  return (
    <span className="border border-border px-1.5 font-mono text-[9px] uppercase tracking-[0.18em] text-muted">
      {children}
    </span>
  );
}

const HANDLE_CLASS =
  "!h-1.5 !w-1.5 !min-w-0 !min-h-0 !border !border-muted !bg-bg";

export interface HoverState {
  dimmed?: boolean;
  highlighted?: boolean;
}

function dimClass(s: HoverState): string {
  return s.dimmed ? "opacity-25 transition-opacity" : "transition-opacity";
}

// --- Input node ------------------------------------------------------------

export interface InputNodeData extends HoverState {
  kind: "input";
  label: string;
  detail: string | null;
  required: boolean;
  orientation: Orientation;
  [key: string]: unknown;
}

/** A single input — a graph node, so dependency and in-group edges
 *  attach at the input level. Lives at rank 0 inside its node-group. */
export function GraphInputNode({ data }: NodeProps) {
  const d = data as InputNodeData;
  const { target, source } = handleSides(d.orientation);
  return (
    <div
      className={[
        "flex h-full items-center justify-between gap-2 border bg-surface px-2.5",
        d.highlighted ? "border-accent" : "border-border",
        dimClass(d),
      ].join(" ")}
    >
      <Handle type="target" position={target} className={HANDLE_CLASS} />
      <Handle type="source" position={source} className={HANDLE_CLASS} />
      <span className="truncate font-sans text-xs text-ink">
        {d.label}
        {d.required ? <span className="text-error"> *</span> : null}
      </span>
      {d.detail ? (
        <span className="shrink-0 font-mono text-[9px] uppercase tracking-[0.12em] text-muted">
          {d.detail}
        </span>
      ) : null}
    </div>
  );
}

// --- Event node (backend / external task / workflow backend) ---------------

export interface EventNodeData extends HoverState {
  kind: "backend" | "workflow_backend";
  label: string;
  detail: string | null;
  isBranch: boolean;
  orientation: Orientation;
  [key: string]: unknown;
}

const EVENT_TAG: Record<EventNodeData["kind"], string> = {
  backend: "On submit",
  workflow_backend: "Workflow step",
};

/** A node-internal backend call or a workflow-level backend step —
 *  drawn as a dashed event box. A node-internal backend is the
 *  terminal node of its node-group, downstream of the inputs that
 *  feed it; a workflow-level backend step stands alone. */
export function GraphEventNode({ data }: NodeProps) {
  const d = data as EventNodeData;
  const { target, source } = handleSides(d.orientation);
  const tag = d.isBranch ? `${EVENT_TAG[d.kind]} · branch` : EVENT_TAG[d.kind];
  return (
    <div
      className={[
        "flex h-full w-full flex-col justify-center border border-dashed bg-bg px-2.5",
        d.highlighted ? "border-accent" : "border-border",
        dimClass(d),
      ].join(" ")}
    >
      <Handle type="target" position={target} className={HANDLE_CLASS} />
      <Handle type="source" position={source} className={HANDLE_CLASS} />
      <span className="font-mono text-[9px] uppercase tracking-[0.16em] text-muted">
        {tag}
      </span>
      <span className="truncate font-mono text-[11px] text-ink">
        {d.label}
      </span>
    </div>
  );
}

// --- Group container -------------------------------------------------------

export interface GroupNodeData extends HoverState {
  title: string;
  isBranch: boolean;
  orientation: Orientation;
  /** Optional per-submission state. Undefined for the structural
   *  form-overview view; set when the canvas is rendering a
   *  specific submission's graph. Drives the group's background
   *  and border color so the per-submission view reads at a glance
   *  like an Airflow DAG-run graph. */
  runState?: "succeeded" | "running" | "failed" | "not_reached";
  [key: string]: unknown;
}

// Run-state classes. Background tints the group, border calls out
// the active step. Failed gets a stronger border to make errors
// jump out from a glance. Not_reached uses a dotted border to read
// as "structural, not yet active" — distinct from a hovered or
// completed group.
const RUN_STATE_CLASSES: Record<
  NonNullable<GroupNodeData["runState"]>,
  { bg: string; border: string }
> = {
  succeeded: { bg: "bg-ink/10", border: "border-ink/40" },
  running: { bg: "bg-accent/15", border: "border-accent" },
  failed: { bg: "bg-error/15", border: "border-error" },
  not_reached: { bg: "bg-surface/40", border: "border-border border-dashed" },
};

/** A node-group — a labeled container enclosing its sub-DAG of inputs,
 *  backend, and external tasks. An edge endpoint itself for the `>>`
 *  execution flow between steps. */
export function GraphGroupNode({ data }: NodeProps) {
  const d = data as GroupNodeData;
  const { target, source } = handleSides(d.orientation);
  const runStyle = d.runState ? RUN_STATE_CLASSES[d.runState] : null;
  // Per-submission coloring takes precedence over the hover border;
  // structural (no runState) keeps the original hover behavior so
  // the form-overview view is unchanged.
  const containerClasses = runStyle
    ? [
        "h-full w-full border",
        runStyle.bg,
        d.highlighted ? "border-accent" : runStyle.border,
        dimClass(d),
      ]
    : [
        "h-full w-full border bg-surface/40",
        d.highlighted ? "border-accent" : "border-border",
        dimClass(d),
      ];
  return (
    <div className={containerClasses.join(" ")}>
      <Handle type="target" position={target} className={HANDLE_CLASS} />
      <Handle type="source" position={source} className={HANDLE_CLASS} />
      <header className="flex items-center gap-1.5 px-3 pt-2.5">
        <span className="font-mono text-[9px] uppercase tracking-[0.2em] text-muted">
          Node
        </span>
        {d.isBranch ? <Tag>Branch</Tag> : null}
        {d.runState ? <RunStateTag state={d.runState} /> : null}
      </header>
      <h3 className="px-3 pt-0.5 font-display text-sm font-semibold leading-tight text-ink">
        {humanize(d.title)}
      </h3>
    </div>
  );
}

function RunStateTag({
  state,
}: {
  state: NonNullable<GroupNodeData["runState"]>;
}) {
  // Small label on the group header — gives a textual cue alongside
  // the color tint so the state is readable for colorblind users
  // and without relying on background contrast alone.
  const label =
    state === "succeeded"
      ? "Done"
      : state === "running"
        ? "Active"
        : state === "failed"
          ? "Failed"
          : "Pending";
  const cls =
    state === "succeeded"
      ? "text-ink"
      : state === "running"
        ? "text-accent"
        : state === "failed"
          ? "text-error"
          : "text-muted";
  return (
    <span
      className={`font-mono text-[9px] uppercase tracking-[0.16em] ${cls}`}
    >
      {label}
    </span>
  );
}

// --- Airflow operator ------------------------------------------------------

export interface AirflowNodeData extends HoverState {
  kind: "airflow";
  label: string;
  /** The Airflow operator kind, e.g. "airflow_trigger_dag". */
  detail: string | null;
  orientation: Orientation;
  [key: string]: unknown;
}

/** Friendly labels for the Airflow operator kinds. */
const AIRFLOW_TAG: Record<string, string> = {
  airflow_trigger_dag: "Trigger DAG",
  airflow_task_sensor: "Poll task",
  airflow_dag_sensor: "Poll DAG",
  airflow_xcom_pull: "Pull XCom",
  airflow_hitl: "Human review",
  airflow_hitl_branch: "Human review · branch",
};

/** A graph-visible Airflow operator — drawn as a solid accent-bordered
 *  box so it reads as live external orchestration, distinct from the
 *  dashed in-process backend boxes. */
export function GraphAirflowNode({ data }: NodeProps) {
  const d = data as AirflowNodeData;
  const { target, source } = handleSides(d.orientation);
  const tag = (d.detail && AIRFLOW_TAG[d.detail]) || "Airflow";
  return (
    <div
      className={[
        "flex h-full w-full flex-col justify-center border bg-bg px-2.5",
        d.highlighted ? "border-accent" : "border-accent",
        dimClass(d),
      ].join(" ")}
    >
      <Handle type="target" position={target} className={HANDLE_CLASS} />
      <Handle type="source" position={source} className={HANDLE_CLASS} />
      <span className="font-mono text-[9px] uppercase tracking-[0.16em] text-accent">
        {tag}
      </span>
      <span className="truncate font-mono text-[11px] text-ink">
        {d.label}
      </span>
    </div>
  );
}

// --- Child cluster (Phase 7e — nested child-graph viz) ---------------------

export interface ChildClusterNodeData extends HoverState {
  /** Display title — typically the child form's title. */
  title: string;
  /** The child form id, used to build the click-through URL. */
  formId: string;
  /** The child submission's handle (stable). */
  submissionHandle: string;
  /** The child submission's minted id, when available. Preferred over
   *  the handle in URLs (it's the canonical, shareable address). */
  submissionId: string | null;
  /** State of the child submission: "running" | "success" | "failed". */
  submissionState: string;
  /** Role granted on the child submission ("reviewer", etc). */
  roleId: string;
  /** Username of the assignee, when resolvable. */
  assigneeUsername: string | null;
  /** True when this assignment has been revoked — the cluster is
   *  rendered muted but still navigable for audit. */
  revoked: boolean;
  /** Depth in the spawn tree — 1 = direct child, 2 = grandchild, etc.
   *  Surfaced in the header only when >1 so the common case stays
   *  uncluttered. */
  depth: number;
  orientation: Orientation;
  [key: string]: unknown;
}

/** A nested cluster node containing one spawned child submission's
 *  graph. The cluster is a React Flow container — its child nodes
 *  are emitted by `layoutGraphWithChildren` with id-prefixed names
 *  (`child:{handle}:{node_id}`) so they don't collide with the
 *  parent's nodes.
 *
 *  Visually a dashed border. Header shows the role granted and the
 *  assignee. Title is a click-through to the child submission's own
 *  page so admins can drill in. */
function GraphChildClusterNode({ data }: NodeProps) {
  const d = data as ChildClusterNodeData;
  const { target, source } = handleSides(d.orientation);
  const stateClass =
    d.submissionState === "success"
      ? "text-muted"
      : d.submissionState === "failed"
        ? "text-error"
        : "text-accent";
  const subId = d.submissionId ?? d.submissionHandle;
  const href = `/forms/${encodeURIComponent(d.formId)}/submissions/${encodeURIComponent(subId)}`;

  // Strict-collapse for revoked clusters (Phase 7f): just the header
  // strip — no inner body region, no nested-node area. The cluster
  // stays in the graph so the audit trail is preserved, but its
  // visual weight is minimized so live work dominates.
  //
  // The layout pass (`layoutGraphWithChildren`) cooperates: it gives
  // revoked clusters a fixed header-height bbox and skips the nested
  // child-graph layout entirely. The `data-cluster-state` attribute
  // is also a bundle-test anchor — looking for "revoked-collapsed"
  // in the shipped JS proves this code path is in the build.
  if (d.revoked) {
    return (
      <div
        data-cluster-state="revoked-collapsed"
        className={[
          "h-full w-full border-2 border-dashed border-border bg-surface/40 opacity-60",
          dimClass(d),
        ].join(" ")}
      >
        <Handle type="target" position={target} className={HANDLE_CLASS} />
        <Handle type="source" position={source} className={HANDLE_CLASS} />
        <header className="flex items-center justify-between gap-3 px-3 pt-2 pb-1.5">
          <div className="flex min-w-0 flex-1 items-center gap-2">
            <span className="font-mono text-[9px] uppercase tracking-[0.2em] text-muted">
              Spawned · {d.roleId}
              {d.depth > 1 ? ` · depth ${d.depth}` : null}
            </span>
            <a
              href={href}
              onClick={(e) => e.stopPropagation()}
              className="font-display text-sm font-semibold leading-tight text-muted line-through hover:text-accent"
            >
              {humanize(d.title)}
            </a>
          </div>
          <div className="flex shrink-0 items-center gap-2 font-mono text-[10px] uppercase tracking-wider">
            {d.assigneeUsername ? (
              <span className="text-muted">→ {d.assigneeUsername}</span>
            ) : null}
            <span className="text-muted">revoked</span>
          </div>
        </header>
      </div>
    );
  }

  return (
    <div
      className={[
        "h-full w-full border-2 border-dashed bg-surface/40 border-accent/40",
        dimClass(d),
      ].join(" ")}
    >
      <Handle type="target" position={target} className={HANDLE_CLASS} />
      <Handle type="source" position={source} className={HANDLE_CLASS} />
      <header className="flex items-center justify-between gap-3 px-3 pt-2 pb-1.5">
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <span className="font-mono text-[9px] uppercase tracking-[0.2em] text-muted">
            Spawned · {d.roleId}
            {d.depth > 1 ? ` · depth ${d.depth}` : null}
          </span>
          <a
            href={href}
            onClick={(e) => e.stopPropagation()}
            className="font-display text-sm font-semibold leading-tight text-ink hover:text-accent"
          >
            {humanize(d.title)}
          </a>
        </div>
        <div className="flex shrink-0 items-center gap-2 font-mono text-[10px] uppercase tracking-wider">
          {d.assigneeUsername ? (
            <span className="text-muted">→ {d.assigneeUsername}</span>
          ) : null}
          <span className={stateClass}>{d.submissionState}</span>
        </div>
      </header>
    </div>
  );
}

export const graphNodeTypes = {
  graphGroup: GraphGroupNode,
  graphInput: GraphInputNode,
  graphEvent: GraphEventNode,
  graphAirflow: GraphAirflowNode,
  childCluster: GraphChildClusterNode,
};
