import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  useNodesState,
  useEdgesState,
  type Node,
  type Edge,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { type ChildGraph, type WorkflowGraph } from "../../lib/api";
import { graphNodeTypes } from "./GraphNodes";
import { graphEdgeTypes } from "./OrthogonalEdge";
import { layoutGraph, type Orientation, type NodeRunState } from "./layout";
import {
  layoutGraphWithChildren,
  type ChildNodeMeta,
} from "./layoutWithChildren";

// --- Hover highlight -------------------------------------------------------

/**
 * The subgraph to emphasize when a node is hovered: the node, the edges
 * touching it (for a node-group, also the edges touching its inputs),
 * and the nodes at the far ends of those edges — plus the parent group
 * of any highlighted input, so containers of involved inputs stay lit.
 *
 * Crosses the cross-form (Assign) boundary in both directions, so a
 * spawning relationship lights up regardless of which end the user
 * hovers:
 *   - Hovering a child cluster includes every namespaced child node
 *     visually inside it (`child:{handle}:*`).
 *   - Hovering a namespaced child node includes its cluster
 *     (`child-cluster:{handle}`), so the parent's `assign-edge` into
 *     the cluster lights up — and via that edge the parent source
 *     node lights up too.
 *   - Hovering a parent node whose `assign-edge` reaches a cluster
 *     also pulls the cluster's inner nodes in, so the entire spawned
 *     subgraph lights up rather than only its wrapper.
 *
 * Exported so it can be unit-tested without a React Flow harness.
 */
export function computeHighlight(
  hoveredId: string,
  nodes: Node[],
  edges: Edge[],
  groupIds: Set<string>,
): { hlNodes: Set<string>; hlEdges: Set<string> } {
  const scope = new Set<string>([hoveredId]);
  if (groupIds.has(hoveredId)) {
    for (const n of nodes) {
      if (n.parentId === hoveredId) scope.add(n.id);
    }
  }

  // Cross-form boundary scoping. The cluster ↔ its inner namespaced
  // nodes form a visual unit — hovering one half pulls the other into
  // the initial scope so the subsequent edge-expansion lights up
  // anything touching either side.
  if (hoveredId.startsWith(CLUSTER_PREFIX)) {
    const handle = hoveredId.slice(CLUSTER_PREFIX.length);
    const childPrefix = `${CHILD_PREFIX}${handle}:`;
    for (const n of nodes) {
      if (n.id.startsWith(childPrefix)) scope.add(n.id);
    }
  } else if (hoveredId.startsWith(CHILD_PREFIX)) {
    const rest = hoveredId.slice(CHILD_PREFIX.length);
    const sep = rest.indexOf(":");
    if (sep > 0) {
      scope.add(`${CLUSTER_PREFIX}${rest.slice(0, sep)}`);
    }
  }

  const hlNodes = new Set<string>(scope);
  const hlEdges = new Set<string>();
  for (const e of edges) {
    if (scope.has(e.source) || scope.has(e.target)) {
      hlEdges.add(e.id);
      hlNodes.add(e.source);
      hlNodes.add(e.target);
    }
  }

  // After edge expansion: if any child-cluster wound up in hlNodes
  // (typically as the target of a freshly-lit `assign-edge`), also
  // pull in its inner namespaced nodes. Without this, hovering a
  // parent node would light up the cluster's empty wrapper while
  // leaving the spawned steps inside it dimmed — visually
  // misleading.
  for (const id of [...hlNodes]) {
    if (id.startsWith(CLUSTER_PREFIX)) {
      const handle = id.slice(CLUSTER_PREFIX.length);
      const childPrefix = `${CHILD_PREFIX}${handle}:`;
      for (const n of nodes) {
        if (n.id.startsWith(childPrefix)) hlNodes.add(n.id);
      }
    }
  }

  for (const id of [...hlNodes]) {
    const sep = id.indexOf("::");
    if (sep >= 0) hlNodes.add(id.slice(0, sep));
  }
  return { hlNodes, hlEdges };
}

// Id-prefix constants matching `layoutWithChildren.ts`. Kept here so
// `computeHighlight` doesn't pull in a layout-module import — the
// prefixes are part of the cross-module id contract, not a
// layout-private detail.
const CLUSTER_PREFIX = "child-cluster:";
const CHILD_PREFIX = "child:";

// --- Canvas ----------------------------------------------------------------

/**
 * The structural graph canvas. Inputs and backend steps are nodes,
 * node-groups are containers, and dependency edges route orthogonally
 * through clear gutter / ring space. Pan/zoom, fit-to-view, an
 * orientation toggle, and hover-to-highlight a node's subgraph.
 *
 * Reused by two views:
 *  - **Form overview** — pure structural diagram, no submission
 *    context, `nodeState`/`onNodeClick` omitted.
 *  - **Per-submission graph** — same renderer with `nodeState`
 *    coloring each top-level node-group by that submission's state
 *    (succeeded / running / failed / not_reached), and
 *    `onNodeClick` wired so clicking a node jumps to its step
 *    block in the page below.
 */
export function WorkflowGraphCanvas({
  graph,
  nodeState,
  onNodeClick,
  childGraphs,
  onChildNodeClick,
  defaultOrientation = "LR",
  orientation: controlledOrientation,
  onOrientationChange,
}: {
  graph: WorkflowGraph;
  nodeState?: Map<string, NodeRunState>;
  onNodeClick?: (nodeId: string) => void;
  /** Spawned child submissions to render as nested clusters. When
   *  provided + non-empty, the canvas switches to the
   *  `layoutGraphWithChildren` wrapper which composes the parent
   *  workflow with one cluster per child submission. Default is no
   *  children (uncluttered single-form view). */
  childGraphs?: ChildGraph[];
  /** Called when a node inside one of the nested child clusters (or
   *  the cluster header itself) is clicked. The meta carries enough
   *  to navigate the user to the child submission + step. */
  onChildNodeClick?: (meta: ChildNodeMeta) => void;
  defaultOrientation?: Orientation;
  /** When provided, the orientation toggle is controlled by the
   *  parent (typically URL-backed). Pair with `onOrientationChange`.
   *  When omitted, the canvas keeps an internal `useState` seeded
   *  by `defaultOrientation` — preserves the simple uncontrolled
   *  API for callers that don't need URL persistence. */
  orientation?: Orientation;
  onOrientationChange?: (o: Orientation) => void;
}) {
  const [internalOrientation, setInternalOrientation] = useState<Orientation>(
    defaultOrientation,
  );
  const isControlled = controlledOrientation !== undefined;
  const orientation = isControlled
    ? controlledOrientation
    : internalOrientation;
  const setOrientation = useCallback(
    (o: Orientation) => {
      if (isControlled) {
        onOrientationChange?.(o);
      } else {
        setInternalOrientation(o);
      }
    },
    [isControlled, onOrientationChange],
  );
  const [hoveredId, setHoveredId] = useState<string | null>(null);

  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  // Per-node metadata for nodes that live inside one of the nested
  // child clusters. Empty when `childGraphs` is empty/undefined, so
  // the single-form view pays nothing for it.
  const [childNodeMeta, setChildNodeMeta] = useState<
    Map<string, ChildNodeMeta>
  >(new Map());

  // Re-lay-out whenever the graph, orientation, state map, or
  // child-graph list changes. The state map is stable per submission
  // (built once from steps), so this only re-runs when the user
  // switches submissions, the submission's step list changes, or a
  // child cluster is granted/revoked.
  useEffect(() => {
    if (childGraphs && childGraphs.length > 0) {
      const laid = layoutGraphWithChildren(
        graph,
        orientation,
        nodeState ?? new Map(),
        childGraphs,
      );
      setNodes(laid.nodes);
      setEdges(laid.edges);
      setChildNodeMeta(laid.childNodeMeta);
    } else {
      const laid = layoutGraph(graph, orientation, nodeState);
      setNodes(laid.nodes);
      setEdges(laid.edges);
      setChildNodeMeta(new Map());
    }
    setHoveredId(null);
  }, [graph, orientation, nodeState, childGraphs, setNodes, setEdges]);

  const groupIds = useMemo(
    () =>
      new Set(
        nodes.filter((n) => n.type === "graphGroup").map((n) => n.id),
      ),
    [nodes],
  );

  // Fold the hover state into node/edge data — dimmed outside the
  // hovered subgraph, highlighted inside it.
  const displayNodes = useMemo(() => {
    if (!hoveredId) {
      return nodes.map((n) => ({
        ...n,
        data: { ...n.data, dimmed: false, highlighted: false },
      }));
    }
    const { hlNodes } = computeHighlight(hoveredId, nodes, edges, groupIds);
    return nodes.map((n) => ({
      ...n,
      data: {
        ...n.data,
        dimmed: !hlNodes.has(n.id),
        highlighted: hlNodes.has(n.id),
      },
    }));
  }, [nodes, edges, hoveredId, groupIds]);

  const displayEdges = useMemo(() => {
    if (!hoveredId) {
      return edges.map((e) => ({
        ...e,
        data: { ...e.data, dimmed: false, highlighted: false },
      }));
    }
    const { hlEdges } = computeHighlight(hoveredId, nodes, edges, groupIds);
    return edges.map((e) => ({
      ...e,
      data: {
        ...e.data,
        dimmed: !hlEdges.has(e.id),
        highlighted: hlEdges.has(e.id),
      },
    }));
  }, [nodes, edges, hoveredId, groupIds]);

  const empty = graph.nodes.length === 0;
  const stepCount = graph.groups.length;
  const depCount = graph.edges.filter(
    (e) => e.relation === "dependency",
  ).length;

  return (
    <div className="border border-border bg-surface">
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <span className="font-mono text-[11px] uppercase tracking-[0.15em] text-muted">
          {stepCount} {stepCount === 1 ? "step" : "steps"}
          {depCount > 0
            ? ` · ${depCount} ${
                depCount === 1 ? "dependency" : "dependencies"
              }`
            : ""}
        </span>
        <div className="flex items-center gap-2">
          <span className="font-mono text-[10px] uppercase tracking-[0.15em] text-muted">
            Layout
          </span>
          <div className="flex border border-border">
            <OrientationButton
              active={orientation === "LR"}
              onClick={() => setOrientation("LR")}
              label="Horizontal"
            />
            <OrientationButton
              active={orientation === "TB"}
              onClick={() => setOrientation("TB")}
              label="Vertical"
            />
          </div>
        </div>
      </div>

      {empty ? (
        <p className="px-4 py-12 text-center font-sans text-sm text-muted">
          This workflow has no steps to show.
        </p>
      ) : (
        <div className="h-[600px] w-full">
          <ReactFlow
            nodes={displayNodes}
            edges={displayEdges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            nodeTypes={graphNodeTypes}
            edgeTypes={graphEdgeTypes}
            onNodeMouseEnter={(_, n) => setHoveredId(n.id)}
            onNodeMouseLeave={() => setHoveredId(null)}
            onNodeClick={
              onNodeClick || onChildNodeClick
                ? (_, n) => {
                    // First: child cluster or any node inside one. A
                    // direct lookup in childNodeMeta (which is keyed
                    // by the cluster id and by every namespaced child
                    // node id) tells us whether the click belongs to
                    // a nested child submission. If so, hand off to
                    // onChildNodeClick — that callback typically
                    // navigates to the child's submission detail page
                    // at the corresponding step.
                    const meta = childNodeMeta.get(n.id);
                    if (meta) {
                      onChildNodeClick?.(meta);
                      return;
                    }
                    if (!onNodeClick) return;
                    // Parent-graph node: only top-level groups are
                    // navigable — inner inputs/backends don't have a
                    // corresponding step block in the page below. The
                    // parent of any inner node is its group's id.
                    const targetId =
                      n.type === "graphGroup" ? n.id : (n.parentId ?? n.id);
                    onNodeClick(targetId);
                  }
                : undefined
            }
            nodesDraggable={false}
            nodesConnectable={false}
            edgesFocusable={false}
            fitView
            minZoom={0.2}
            maxZoom={1.5}
            proOptions={{ hideAttribution: true }}
          >
            <Background
              gap={20}
              size={1}
              color="rgb(var(--color-border))"
            />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>
      )}

      <GraphLegend />
    </div>
  );
}

function OrientationButton({
  active,
  onClick,
  label,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        "px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.12em] transition-colors",
        active ? "bg-ink text-bg" : "bg-surface text-muted hover:text-ink",
      ].join(" ")}
    >
      {label}
    </button>
  );
}

/** A compact key for the three edge relations. */
function GraphLegend() {
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5 border-t border-border px-4 py-2.5">
      <LegendItem label="Execution flow">
        <svg width="28" height="8" aria-hidden>
          <line
            x1="0"
            y1="4"
            x2="28"
            y2="4"
            stroke="rgb(var(--color-ink))"
            strokeWidth="1.4"
            opacity="0.5"
          />
        </svg>
      </LegendItem>
      <LegendItem label="In-group flow">
        <svg width="28" height="8" aria-hidden>
          <line
            x1="0"
            y1="4"
            x2="28"
            y2="4"
            stroke="rgb(var(--color-muted))"
            strokeWidth="1"
            opacity="0.6"
          />
        </svg>
      </LegendItem>
      <LegendItem label="Dependency · functional">
        <svg width="28" height="8" aria-hidden>
          <line
            x1="0"
            y1="4"
            x2="28"
            y2="4"
            stroke="rgb(var(--color-accent))"
            strokeWidth="1.7"
          />
        </svg>
      </LegendItem>
      <LegendItem label="Dependency · display">
        <svg width="28" height="8" aria-hidden>
          <line
            x1="0"
            y1="4"
            x2="28"
            y2="4"
            stroke="rgb(var(--color-accent))"
            strokeWidth="1.3"
            strokeDasharray="5 3"
            opacity="0.65"
          />
        </svg>
      </LegendItem>
      <LegendItem label="Airflow operator">
        <svg width="28" height="14" aria-hidden>
          <rect
            x="1"
            y="1"
            width="26"
            height="12"
            fill="rgb(var(--color-bg))"
            stroke="rgb(var(--color-accent))"
            strokeWidth="1.3"
          />
        </svg>
      </LegendItem>
      <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-muted">
        Hover a node to trace it
      </span>
    </div>
  );
}

function LegendItem({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <span className="flex items-center gap-2">
      {children}
      <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-muted">
        {label}
      </span>
    </span>
  );
}
