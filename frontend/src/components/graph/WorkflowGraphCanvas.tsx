import { useEffect, useMemo, useState } from "react";
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
import { type WorkflowGraph } from "../../lib/api";
import { graphNodeTypes } from "./GraphNodes";
import { graphEdgeTypes } from "./OrthogonalEdge";
import { layoutGraph, type Orientation } from "./layout";

// --- Hover highlight -------------------------------------------------------

/**
 * The subgraph to emphasize when a node is hovered: the node, the edges
 * touching it (for a node-group, also the edges touching its inputs),
 * and the nodes at the far ends of those edges — plus the parent group
 * of any highlighted input, so containers of involved inputs stay lit.
 */
function computeHighlight(
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
  const hlNodes = new Set<string>(scope);
  const hlEdges = new Set<string>();
  for (const e of edges) {
    if (scope.has(e.source) || scope.has(e.target)) {
      hlEdges.add(e.id);
      hlNodes.add(e.source);
      hlNodes.add(e.target);
    }
  }
  for (const id of [...hlNodes]) {
    const sep = id.indexOf("::");
    if (sep >= 0) hlNodes.add(id.slice(0, sep));
  }
  return { hlNodes, hlEdges };
}

// --- Canvas ----------------------------------------------------------------

/**
 * The structural graph canvas. Inputs and backend steps are nodes,
 * node-groups are containers, and dependency edges route orthogonally
 * through clear gutter / ring space. Pan/zoom, fit-to-view, an
 * orientation toggle, and hover-to-highlight a node's subgraph.
 */
export function WorkflowGraphCanvas({ graph }: { graph: WorkflowGraph }) {
  const [orientation, setOrientation] = useState<Orientation>("LR");
  const [hoveredId, setHoveredId] = useState<string | null>(null);

  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);

  // Re-lay-out whenever the graph or orientation changes.
  useEffect(() => {
    const laid = layoutGraph(graph, orientation);
    setNodes(laid.nodes);
    setEdges(laid.edges);
    setHoveredId(null);
  }, [graph, orientation, setNodes, setEdges]);

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
