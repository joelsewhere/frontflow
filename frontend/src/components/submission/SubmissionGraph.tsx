import { useMemo } from "react";
import type { ChildGraph, StepDetailRow } from "../../lib/api";
import { useFormGraph } from "../../hooks/useFormGraph";
import { WorkflowGraphCanvas } from "../graph/WorkflowGraphCanvas";
import type { NodeRunState } from "../graph/layout";
import type { ChildNodeMeta } from "../graph/layoutWithChildren";

/**
 * Per-submission graph view — reuses the form-overview graph
 * renderer (`WorkflowGraphCanvas`) and overlays this submission's
 * per-step state as node-group coloring. Same data source as the
 * form-summary viz; the only addition is the state map built from
 * the submission's step list.
 *
 * Clicking a node-group in the parent's graph jumps to the
 * corresponding step block in the Steps list rendered below (the
 * parent passes `onNodeClick` which scrolls + highlights that block).
 *
 * When `childGraphs` is provided (the parent submission spawned
 * child submissions via Assign), they're rendered as nested clusters
 * to the right (LR) or below (TB) the parent workflow. Clicking a
 * cluster or any node inside one calls `onChildNodeClick` so the
 * parent page can navigate to the child submission's detail view.
 */
export function SubmissionGraph({
  formId,
  steps,
  onNodeClick,
  childGraphs,
  onChildNodeClick,
  orientation,
  onOrientationChange,
}: {
  formId: string;
  steps: StepDetailRow[];
  onNodeClick: (nodeId: string) => void;
  childGraphs?: ChildGraph[];
  onChildNodeClick?: (meta: ChildNodeMeta) => void;
  orientation?: "LR" | "TB";
  onOrientationChange?: (o: "LR" | "TB") => void;
}) {
  const { data: graph, error, isLoading } = useFormGraph(formId);

  // Map node_id → run state. The form graph contains every node;
  // the submission's steps cover only the nodes it reached. Nodes
  // not in the step list are `not_reached` by default — set when
  // we don't find them. The map is built once from the steps and
  // passed through to the renderer.
  const nodeState = useMemo(() => {
    const m = new Map<string, NodeRunState>();
    for (const s of steps) {
      // step.state: "awaiting" | "submitted" | "failed"
      if (s.state === "submitted") m.set(s.node_id, "succeeded");
      else if (s.state === "awaiting") m.set(s.node_id, "running");
      else if (s.state === "failed") m.set(s.node_id, "failed");
    }
    return m;
  }, [steps]);

  if (isLoading) {
    return (
      <p className="text-muted text-sm">Loading graph…</p>
    );
  }
  if (error) {
    return (
      <p className="text-error text-sm">
        Couldn't load the graph.
      </p>
    );
  }
  if (!graph) return null;
  return (
    <WorkflowGraphCanvas
      graph={graph}
      nodeState={nodeState}
      onNodeClick={onNodeClick}
      childGraphs={childGraphs}
      onChildNodeClick={onChildNodeClick}
      orientation={orientation}
      onOrientationChange={onOrientationChange}
    />
  );
}
