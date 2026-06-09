/**
 * Layout wrapper that composes the parent workflow's React Flow nodes
 * with one nested cluster per spawned child submission.
 *
 * The parent is laid out by the existing `layoutGraph` function. For
 * each `ChildGraph` in the parent's `/detail` payload, this wrapper:
 *
 *   1. Lays out the child's static workflow graph (recursively via
 *      `layoutGraph`) with the child submission's per-node run state.
 *   2. Prefixes every child node id with `child:{handle}:` so it can't
 *      collide with parent nodes (or sibling children's nodes).
 *   3. Translates the child's node positions to sit alongside the
 *      parent — to the right for LR layouts, below for TB layouts.
 *   4. Emits a `child-cluster:{handle}` container node that wraps the
 *      child's nodes. This container is rendered by
 *      `GraphChildClusterNode` from `./GraphNodes` and is the
 *      clickable handle for navigating to the child submission.
 *   5. Emits a dashed `assign-edge:{assignmentId}` edge from the
 *      parent's node (the one that spawned this child) to the
 *      cluster, labeled with the granted role. Animated unless the
 *      grant has been revoked.
 *
 * Grandchildren (child.depth > 1) attach their assign-edge to a node
 * inside an earlier cluster, looked up by submission handle.
 *
 * Returns the merged React Flow node/edge lists, a merged nodeState
 * map (with prefixed keys for child nodes), and a `childNodeMeta` map
 * keyed by every child node's namespaced id so the canvas's click
 * handler can route a click into the parent's graph view to the
 * corresponding child submission + step.
 */

import type { Edge, Node } from "@xyflow/react";
import type { ChildGraph } from "../../lib/api";
import {
  layoutGraph,
  type NodeRunState,
  type Orientation,
} from "./layout";

// Spacing constants — kept narrow so children sit close to the parent
// but with enough breathing room to read the cluster header.
const GAP_TB = 120; //   between parent and first child, top-to-bottom orientation
const GAP_LR = 200; //   between parent and first child, left-to-right orientation
const CLUSTER_PADDING = 24; // padding inside the cluster around the child's bbox
const HEADER_HEIGHT = 40; //  vertical room reserved for the cluster header
// Phase 7f — revoked clusters strict-collapse to a fixed header strip.
// We don't lay out their child graph; the strip's just enough to read
// the title and role, and to anchor any grandchildren spawned through
// it. Width is wide enough for the title; height matches HEADER_HEIGHT.
const REVOKED_STRIP_WIDTH = 320;
const REVOKED_STRIP_HEIGHT = HEADER_HEIGHT;

/** Per-namespaced-node metadata so the canvas's click handler can
 *  route a click in the parent's graph view to the right child
 *  submission + step. */
export interface ChildNodeMeta {
  childFormId: string;
  childHandle: string;
  childSubmissionId: string | null;
  /** The original (un-namespaced) node id inside the child graph — the
   *  step the user clicked. */
  childStepId: string;
}

export interface LayoutWithChildrenResult {
  nodes: Node[];
  edges: Edge[];
  /** Merged per-node run state — same shape as `layoutGraph`'s input,
   *  but with `child:{handle}:` prefixes on child nodes so the canvas
   *  hover-highlighting logic can include them. */
  nodeState: Map<string, NodeRunState>;
  /** Lookup: namespaced node id → which child submission + step it
   *  belongs to. Also keyed by the cluster id (`child-cluster:{handle}`)
   *  so a click on the cluster itself routes to the child submission's
   *  landing step. */
  childNodeMeta: Map<string, ChildNodeMeta>;
}

interface Bbox {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** Compute the axis-aligned bounding box of a set of React Flow nodes.
 *
 *  Falls back to a small placeholder bbox when there are no nodes (an
 *  empty child graph still needs *some* footprint so its cluster is
 *  visible and clickable). */
function bboxOf(nodes: Node[]): Bbox {
  let xMin = Infinity;
  let yMin = Infinity;
  let xMax = -Infinity;
  let yMax = -Infinity;
  for (const n of nodes) {
    const w =
      typeof n.style?.width === "number"
        ? n.style.width
        : typeof n.width === "number"
          ? n.width
          : 0;
    const h =
      typeof n.style?.height === "number"
        ? n.style.height
        : typeof n.height === "number"
          ? n.height
          : 0;
    if (n.position.x < xMin) xMin = n.position.x;
    if (n.position.y < yMin) yMin = n.position.y;
    if (n.position.x + w > xMax) xMax = n.position.x + w;
    if (n.position.y + h > yMax) yMax = n.position.y + h;
  }
  if (xMin === Infinity) return { x: 0, y: 0, w: 120, h: 60 };
  return { x: xMin, y: yMin, w: xMax - xMin, h: yMax - yMin };
}

/** Find the React Flow node id that corresponds to a backend node id
 *  in the parent's layout. The parent's `layoutGraph` may have wrapped
 *  workflow nodes in groups, so the direct id may not match — fall
 *  back to a node whose id ends with `:{nodeId}` (the group::member
 *  convention) or to the group id itself. */
function findParentNodeId(
  parentNodes: Node[],
  backendNodeId: string,
): string | null {
  // Direct hit first.
  const direct = parentNodes.find((n) => n.id === backendNodeId);
  if (direct) return direct.id;
  // Group::member fallback.
  const grouped = parentNodes.find(
    (n) =>
      n.id.endsWith(`::${backendNodeId}`) ||
      n.id === `group:${backendNodeId}`,
  );
  return grouped ? grouped.id : null;
}

/** Prefix every node id (and its parentId, if any) plus every edge's
 *  source/target with the per-child namespace. */
function prefixIds(
  nodes: Node[],
  edges: Edge[],
  prefix: string,
): { nodes: Node[]; edges: Edge[] } {
  const ns = (id: string) => `${prefix}${id}`;
  return {
    nodes: nodes.map((n) => ({
      ...n,
      id: ns(n.id),
      parentId: n.parentId ? ns(n.parentId) : n.parentId,
    })),
    edges: edges.map((e) => ({
      ...e,
      id: ns(e.id),
      source: ns(e.source),
      target: ns(e.target),
    })),
  };
}

export function layoutGraphWithChildren(
  parentGraph: Parameters<typeof layoutGraph>[0],
  orientation: Orientation,
  nodeState: Map<string, NodeRunState>,
  childGraphs: ChildGraph[],
): LayoutWithChildrenResult {
  // Lay out the parent first. Its bbox decides where the first child
  // cluster goes — to the right for LR, below for TB.
  const parent = layoutGraph(parentGraph, orientation, nodeState);
  const mergedNodeState = new Map(nodeState);
  const childNodeMeta = new Map<string, ChildNodeMeta>();

  if (childGraphs.length === 0) {
    return {
      nodes: parent.nodes,
      edges: parent.edges,
      nodeState: mergedNodeState,
      childNodeMeta,
    };
  }

  const isLR = orientation === "LR";
  const parentBbox = bboxOf(parent.nodes);

  const allNodes: Node[] = [...parent.nodes];
  const allEdges: Edge[] = [...parent.edges];
  // Track each laid-out child so a grandchild can locate the cluster
  // its parent_submission_handle lives in.
  const laidOutChildren = new Set<string>();
  // Phase 7f — track which laid-out clusters are strict-collapsed
  // (revoked). A grandchild whose parent is in this set anchors its
  // assign-edge to the cluster id itself rather than to an inner node
  // (because we never emitted any).
  const revokedClusters = new Set<string>();

  // ---- Tree-recursive layout (#3) ----
  //
  // The previous implementation walked `childGraphs` as a flat list and
  // pushed each cluster along a single mainAxis cursor. That worked
  // for "all clusters at depth 1" but laid grandchildren out next to
  // their grandparent's root rather than their own direct parent — so
  // a 3-level tree visually looked like 3 disconnected rows.
  //
  // This version builds an explicit subtree per direct child, sizes it
  // bottom-up (each subtree's outer bbox is `cluster + its own subtree
  // column`), then places top-down. Siblings stack along the axis
  // perpendicular to the flow (Y for LR, X for TB); each cluster's
  // subchildren start at its trailing edge plus a flow-axis gap.
  const byParentHandle = new Map<string, ChildGraph[]>();
  const roots: ChildGraph[] = [];
  for (const c of childGraphs) {
    if (c.depth === 1) {
      roots.push(c);
    } else {
      let bucket = byParentHandle.get(c.parent_submission_handle);
      if (!bucket) {
        bucket = [];
        byParentHandle.set(c.parent_submission_handle, bucket);
      }
      bucket.push(c);
    }
  }

  /** Per-subtree intermediate carrying inner geometry + recursive sub-
   *  trees. `outerW` / `outerH` are filled by the sizing pass. */
  interface PreparedSubtree {
    child: ChildGraph;
    isRevoked: boolean;
    clusterW: number;
    clusterH: number;
    /** Inner namespaced child-graph nodes, positioned RELATIVE to
     *  the cluster's local (0, 0). Translation to absolute coords
     *  happens in the placement pass. Empty for revoked clusters. */
    innerNodes: Node[];
    innerEdges: Edge[];
    /** Per-cluster run state, for merging into mergedNodeState
     *  during placement. */
    childState: Map<string, NodeRunState> | null;
    subTrees: PreparedSubtree[];
    /** Outer bbox dimensions including subTrees. */
    outerW: number;
    outerH: number;
  }

  /** Recursive build: lay out one child's own form-graph, recurse into
   *  its subchildren, then compute its outer bbox. */
  function prepareSubtree(child: ChildGraph): PreparedSubtree {
    const isRevoked = child.revoked_at !== null;
    let clusterW: number;
    let clusterH: number;
    let innerNodes: Node[] = [];
    let innerEdges: Edge[] = [];
    let childState: Map<string, NodeRunState> | null = null;
    if (isRevoked) {
      clusterW = REVOKED_STRIP_WIDTH;
      clusterH = REVOKED_STRIP_HEIGHT;
    } else {
      childState = new Map<string, NodeRunState>();
      for (const [k, v] of Object.entries(child.node_state)) {
        childState.set(k, v as NodeRunState);
      }
      const childLayout = layoutGraph(child.graph, orientation, childState);
      const childBbox = bboxOf(childLayout.nodes);
      // Translate inner nodes so the cluster's local (0,0) is the
      // top-left of the cluster wrapper; padding + header offset
      // bring the inner graph inside the wrapper.
      const dx = CLUSTER_PADDING - childBbox.x;
      const dy = CLUSTER_PADDING + HEADER_HEIGHT - childBbox.y;
      const prefix = `child:${child.child_submission_handle}:`;
      const namespaced = prefixIds(
        childLayout.nodes,
        childLayout.edges,
        prefix,
      );
      innerNodes = namespaced.nodes.map((n) => ({
        ...n,
        position: { x: n.position.x + dx, y: n.position.y + dy },
      }));
      innerEdges = namespaced.edges;
      clusterW = childBbox.w + CLUSTER_PADDING * 2;
      clusterH = childBbox.h + CLUSTER_PADDING * 2 + HEADER_HEIGHT;
    }

    // Recurse — direct subchildren of this child.
    const directKids =
      byParentHandle.get(child.child_submission_handle) ?? [];
    const subTrees = directKids.map(prepareSubtree);

    // Outer bbox: cluster + (subtree column, if any). The subtree
    // column is perpendicular to the flow axis — siblings stack
    // along the cross axis at a constant flow-axis offset.
    let outerW: number;
    let outerH: number;
    if (subTrees.length === 0) {
      outerW = clusterW;
      outerH = clusterH;
    } else if (isLR) {
      const stackH =
        subTrees.reduce((acc, s) => acc + s.outerH, 0) +
        (subTrees.length - 1) * GAP_TB;
      const stackW = Math.max(...subTrees.map((s) => s.outerW));
      outerW = clusterW + GAP_LR + stackW;
      outerH = Math.max(clusterH, stackH);
    } else {
      const stackW =
        subTrees.reduce((acc, s) => acc + s.outerW, 0) +
        (subTrees.length - 1) * GAP_LR;
      const stackH = Math.max(...subTrees.map((s) => s.outerH));
      outerW = Math.max(clusterW, stackW);
      outerH = clusterH + GAP_TB + stackH;
    }
    return {
      child,
      isRevoked,
      clusterW,
      clusterH,
      innerNodes,
      innerEdges,
      childState,
      subTrees,
      outerW,
      outerH,
    };
  }

  /** Walk one subtree at absolute (x, y) — emit cluster, translate
   *  inner nodes, emit assign-edge from `parentAnchorId` (the parent
   *  graph's source node for depth==1, or the direct parent cluster's
   *  inner node for deeper, falling back to the cluster id itself for
   *  revoked-parent paths), then recurse into its subTrees. */
  function placeSubtree(
    st: PreparedSubtree,
    x: number,
    y: number,
    parentAnchorId: string | null,
  ): void {
    const child = st.child;
    const clusterId = `child-cluster:${child.child_submission_handle}`;

    // Cluster wrapper node.
    allNodes.push({
      id: clusterId,
      type: "childCluster",
      position: { x, y },
      style: { width: st.clusterW, height: st.clusterH },
      zIndex: -1,
      data: {
        title: child.child_form_title,
        formId: child.child_form_id,
        submissionHandle: child.child_submission_handle,
        submissionId: child.child_submission_id,
        submissionState: child.child_submission_state,
        roleId: child.role_id,
        assigneeUsername: child.assignee_username,
        revoked: st.isRevoked,
        depth: child.depth,
        orientation,
      },
      draggable: false,
      selectable: false,
    });

    // Translate this cluster's inner nodes into absolute coords.
    for (const n of st.innerNodes) {
      allNodes.push({
        ...n,
        position: { x: n.position.x + x, y: n.position.y + y },
      });
    }
    for (const e of st.innerEdges) allEdges.push(e);

    // Assign-edge from this cluster's parent into the cluster.
    if (parentAnchorId) {
      allEdges.push({
        id: `assign-edge:${child.assignment_id}`,
        source: parentAnchorId,
        target: clusterId,
        type: "default",
        data: {
          kind: "assign",
          roleId: child.role_id,
          revoked: st.isRevoked,
        },
        style: {
          strokeDasharray: "6 4",
          stroke: st.isRevoked ? "var(--muted)" : "var(--accent)",
          strokeWidth: 1.5,
        },
        animated: !st.isRevoked,
        label: child.role_id,
        labelStyle: {
          fontSize: 10,
          fontFamily: "monospace",
          fill: "var(--muted)",
          textTransform: "uppercase",
          letterSpacing: "0.05em",
        },
        labelBgStyle: { fill: "var(--bg)" },
        labelBgPadding: [4, 2],
      });
    }

    // Merge run state into the combined map. Revoked clusters
    // have no inner nodes — nothing to merge.
    if (st.childState) {
      const prefix = `child:${child.child_submission_handle}:`;
      for (const [k, v] of st.childState) {
        mergedNodeState.set(`${prefix}${k}`, v);
      }
    }

    // Click metadata — the cluster itself plus every inner node.
    childNodeMeta.set(clusterId, {
      childFormId: child.child_form_id,
      childHandle: child.child_submission_handle,
      childSubmissionId: child.child_submission_id,
      childStepId: "",
    });
    const prefix = `child:${child.child_submission_handle}:`;
    for (const n of st.innerNodes) {
      const originalId = n.id.slice(prefix.length);
      childNodeMeta.set(n.id, {
        childFormId: child.child_form_id,
        childHandle: child.child_submission_handle,
        childSubmissionId: child.child_submission_id,
        childStepId: originalId,
      });
    }

    laidOutChildren.add(child.child_submission_handle);
    if (st.isRevoked) {
      revokedClusters.add(child.child_submission_handle);
    }

    // Recurse into subtrees. They stack on the cross axis at the
    // cluster's trailing flow-axis edge plus GAP_*. Each grandchild's
    // assign-edge anchors to a node INSIDE this cluster — its
    // `parent_node_id` — or to this cluster itself if it's revoked.
    if (st.subTrees.length > 0) {
      let cursorX: number;
      let cursorY: number;
      if (isLR) {
        cursorX = x + st.clusterW + GAP_LR;
        cursorY = y;
      } else {
        cursorX = x;
        cursorY = y + st.clusterH + GAP_TB;
      }
      for (const ss of st.subTrees) {
        // Anchor for this grandchild's assign-edge.
        let anchorId: string | null;
        if (st.isRevoked) {
          // Parent cluster has no inner nodes — anchor on the
          // cluster wrapper itself so the spawn chain remains
          // visually continuous.
          anchorId = clusterId;
        } else {
          anchorId = `child:${child.child_submission_handle}:${ss.child.parent_node_id}`;
        }
        placeSubtree(ss, cursorX, cursorY, anchorId);
        if (isLR) cursorY += ss.outerH + GAP_TB;
        else cursorX += ss.outerW + GAP_LR;
      }
    }
  }

  // Build subtrees for the depth-1 roots and lay them out next to
  // the parent workflow. They stack on the cross-axis (Y for LR,
  // X for TB) starting at the parent's leading cross-axis edge.
  const rootSubtrees = roots.map(prepareSubtree);
  if (rootSubtrees.length > 0) {
    let rootX: number;
    let rootY: number;
    if (isLR) {
      rootX = parentBbox.x + parentBbox.w + GAP_LR;
      rootY = parentBbox.y;
    } else {
      rootX = parentBbox.x;
      rootY = parentBbox.y + parentBbox.h + GAP_TB;
    }
    for (const st of rootSubtrees) {
      // Depth-1 anchor: the parent's source node from where the
      // Assign fired.
      const anchor = findParentNodeId(
        parent.nodes, st.child.parent_node_id,
      );
      placeSubtree(st, rootX, rootY, anchor);
      if (isLR) rootY += st.outerH + GAP_TB;
      else rootX += st.outerW + GAP_LR;
    }
  }

  return {
    nodes: allNodes,
    edges: allEdges,
    nodeState: mergedNodeState,
    childNodeMeta,
  };
}
