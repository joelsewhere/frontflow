import { type Node, type Edge } from "@xyflow/react";
import {
  type WorkflowGraph,
  type GraphNode,
  type GraphEdge,
} from "../../lib/api";
import { type Orientation } from "./GraphNodes";

export type { Orientation };

// --- Box geometry ----------------------------------------------------------
// Boxes are W×H as drawn. The "main" axis is the workflow's flow
// direction; "cross" is perpendicular. LR: main=x. TB: main=y.

const BOX = {
  input: { w: 190, h: 30 },
  backend: { w: 156, h: 46 },
  airflow: { w: 184, h: 46 },
  workflow_backend: { w: 184, h: 58 },
} as const;

const INPUT_GAP = 6; // between stacked inputs (cross axis)
const RANK_GAP = 60; // between in-group ranks (main axis)
const GROUP_PAD = 14;
const GROUP_HEADER = 44;
const LANE_GAP = 150; // gutter between columns (main axis)
const RING_OFFSET = 64; // ring clearance past the group band (cross)
const RING_STEP = 24;
const EXIT_OFF = 20; // how far past a node its exit lane sits

function mainExtent(n: GraphNode, isLR: boolean): number {
  const b = BOX[n.kind];
  return isLR ? b.w : b.h;
}
function crossExtent(n: GraphNode, isLR: boolean): number {
  const b = BOX[n.kind];
  return isLR ? b.h : b.w;
}

// --- Routed-edge payload ---------------------------------------------------

export interface EdgeRoute {
  orientation: Orientation;
  kind: "direct" | "ring";
  /** Direct: the main-axis line the edge bends on. */
  mid: number;
  /** Ring: main-axis exit lane, cross-axis ring lane, main-axis entry. */
  lane1: number;
  ring: number;
  lane2: number;
  relation: GraphEdge["relation"];
  functional: boolean;
}

export interface GraphEdgeData {
  route: EdgeRoute;
  dimmed: boolean;
  highlighted: boolean;
  [key: string]: unknown;
}

// --- Column ordering -------------------------------------------------------

/** Rank the top-level units (node-groups + workflow-level backend
 *  steps) by depth — the longest execution path from an entry unit.
 *  Units at the same depth share a main-axis column; returns the units
 *  bucketed by depth, each bucket in original declaration order so
 *  branch siblings keep a stable cross-axis order. */
function rankColumns(
  unitIds: string[],
  execEdges: GraphEdge[],
): string[][] {
  const known = new Set(unitIds);
  const adj = new Map<string, string[]>(unitIds.map((id) => [id, []]));
  const indeg = new Map<string, number>(unitIds.map((id) => [id, 0]));
  for (const e of execEdges) {
    if (!known.has(e.from_node) || !known.has(e.to_node)) continue;
    adj.get(e.from_node)!.push(e.to_node);
    indeg.set(e.to_node, (indeg.get(e.to_node) ?? 0) + 1);
  }
  // Longest-path depth via a Kahn pass: a unit's depth is one past the
  // deepest upstream unit. Topologically safe for a DAG.
  const depth = new Map<string, number>(unitIds.map((id) => [id, 0]));
  const work = new Map(indeg);
  const queue = unitIds.filter((id) => (work.get(id) ?? 0) === 0);
  const seen = new Set<string>();
  while (queue.length) {
    const id = queue.shift()!;
    if (seen.has(id)) continue;
    seen.add(id);
    for (const nxt of adj.get(id) ?? []) {
      depth.set(nxt, Math.max(depth.get(nxt) ?? 0, (depth.get(id) ?? 0) + 1));
      work.set(nxt, (work.get(nxt) ?? 0) - 1);
      if ((work.get(nxt) ?? 0) === 0) queue.push(nxt);
    }
  }
  // Any unit not reached (a cycle, or disconnected) lands at depth 0.
  const maxDepth = Math.max(0, ...unitIds.map((id) => depth.get(id) ?? 0));
  const buckets: string[][] = Array.from({ length: maxDepth + 1 }, () => []);
  for (const id of unitIds) buckets[depth.get(id) ?? 0].push(id);
  return buckets;
}

// --- Layout ----------------------------------------------------------------

interface Box {
  mainStart: number;
  mainEnd: number;
  crossStart: number;
  crossMid: number;
}

/**
 * Lay the workflow out as a DAG of input and backend nodes.
 *
 * Each node-group is a container holding a small sub-DAG: inputs at
 * rank 0 and the node-internal backend at rank 1, downstream of the
 * inputs that feed it — flowing the same direction as the overall
 * workflow. Node-groups and workflow-level backend steps are columns
 * along the main axis, in execution order.
 *
 * Edges are routed orthogonally: in-group edges bend once in the rank
 * gap; execution edges bend once in a column gutter; dependency edges
 * route out through a clear rank gap, across the ring channel above
 * the band, and back down — never crossing a group they don't belong
 * to, every elbow outside a group box.
 */
export function layoutGraph(
  graph: WorkflowGraph,
  orientation: Orientation,
): { nodes: Node[]; edges: Edge[] } {
  const isLR = orientation === "LR";
  const proj = (main: number, cross: number) =>
    isLR ? { x: main, y: cross } : { x: cross, y: main };

  // Members by group; standalone workflow-backend nodes.
  const membersByGroup = new Map<string, GraphNode[]>();
  const wbNodes: GraphNode[] = [];
  for (const n of graph.nodes) {
    if (n.kind === "workflow_backend") {
      wbNodes.push(n);
    } else if (n.group_id) {
      const arr = membersByGroup.get(n.group_id) ?? [];
      arr.push(n);
      membersByGroup.set(n.group_id, arr);
    }
  }
  const groupById = new Map(graph.groups.map((g) => [g.id, g]));

  // Column ranks — groups + workflow-backend steps, bucketed by depth.
  const execEdges = graph.edges.filter((e) => e.relation === "execution");
  const unitIds = [
    ...graph.groups.map((g) => g.id),
    ...wbNodes.map((n) => n.id),
  ];
  const columnRanks = rankColumns(unitIds, execEdges);
  const columnOrder = columnRanks.flat();

  // --- Size each column (group or workflow-backend) ---
  interface ColumnPlan {
    id: string;
    isGroup: boolean;
    mainSize: number;
    crossSize: number;
    // Per-member relative (main, cross) within the column, header-less.
    memberPos: Map<string, { main: number; cross: number }>;
  }

  const plans = new Map<string, ColumnPlan>();
  for (const id of columnOrder) {
    const group = groupById.get(id);
    if (!group) {
      // A workflow-backend column — a single node.
      const wb = wbNodes.find((n) => n.id === id)!;
      plans.set(id, {
        id,
        isGroup: false,
        mainSize: mainExtent(wb, isLR),
        crossSize: crossExtent(wb, isLR),
        memberPos: new Map([[id, { main: 0, cross: 0 }]]),
      });
      continue;
    }
    const members = (membersByGroup.get(id) ?? [])
      .slice()
      .sort((a, b) => a.rank - b.rank);
    const byRank = new Map<number, GraphNode[]>();
    for (const m of members) {
      const arr = byRank.get(m.rank) ?? [];
      arr.push(m);
      byRank.set(m.rank, arr);
    }
    const ranks = [...byRank.keys()].sort((a, b) => a - b);

    // Each rank's cross extent (members stacked along cross).
    const rankCross = new Map<number, number>();
    for (const r of ranks) {
      const ms = byRank.get(r)!;
      const total =
        ms.reduce((s, m) => s + crossExtent(m, isLR), 0) +
        INPUT_GAP * Math.max(ms.length - 1, 0);
      rankCross.set(r, total);
    }
    const contentCross = Math.max(1, ...rankCross.values());

    // Place ranks along the main axis; members centered along cross.
    const memberPos = new Map<string, { main: number; cross: number }>();
    let mainCursor = GROUP_PAD;
    for (const r of ranks) {
      const ms = byRank.get(r)!;
      const rankMain = Math.max(...ms.map((m) => mainExtent(m, isLR)));
      let crossCursor = (contentCross - rankCross.get(r)!) / 2;
      for (const m of ms) {
        memberPos.set(m.id, {
          main: mainCursor + (rankMain - mainExtent(m, isLR)) / 2,
          cross: crossCursor,
        });
        crossCursor += crossExtent(m, isLR) + INPUT_GAP;
      }
      mainCursor += rankMain + RANK_GAP;
    }
    const mainSize = mainCursor - RANK_GAP + GROUP_PAD;
    plans.set(id, {
      id,
      isGroup: true,
      mainSize,
      crossSize: contentCross + GROUP_PAD,
      memberPos,
    });
  }

  // --- Place columns by depth rank, siblings spread on the cross axis ---
  // Cross extent of one unit, including a group's header.
  const unitCross = (id: string): number => {
    const p = plans.get(id)!;
    return p.crossSize + (p.isGroup ? GROUP_HEADER : 0);
  };
  // A rank's total cross extent — its units stacked with SIBLING_GAP.
  const SIBLING_GAP = LANE_GAP;
  const rankCrossTotal = (rank: string[]): number =>
    rank.reduce((sum, id) => sum + unitCross(id), 0) +
    Math.max(0, rank.length - 1) * SIBLING_GAP;
  const maxCross = Math.max(1, ...columnRanks.map(rankCrossTotal));

  const nodes: Node[] = [];
  const colStart = new Map<string, number>(); // main-axis start
  const colCrossStart = new Map<string, number>();
  const absBox = new Map<string, Box>(); // every node id → abs box

  let mainCursor = 0;
  for (const rank of columnRanks) {
    if (rank.length === 0) continue;
    // This rank's column width — the widest unit in it.
    const colMain = Math.max(...rank.map((id) => plans.get(id)!.mainSize));
    // Stack the rank's units along the cross axis, the stack centered.
    let crossCursor = (maxCross - rankCrossTotal(rank)) / 2;

    for (const id of rank) {
      const plan = plans.get(id)!;
      const headed = plan.isGroup ? GROUP_HEADER : 0;
      const fullCross = plan.crossSize + headed;
      const crossStart = crossCursor;
      colStart.set(id, mainCursor);
      colCrossStart.set(id, crossStart);

      if (plan.isGroup) {
        const group = groupById.get(id)!;
        const pos = proj(mainCursor, crossStart);
        nodes.push({
          id,
          type: "graphGroup",
          position: pos,
          style: isLR
            ? { width: plan.mainSize, height: fullCross }
            : { width: fullCross, height: plan.mainSize },
          data: {
            title: group.title,
            isLanding: group.is_landing,
            isBranch: group.is_branch,
            orientation,
          },
        });
        absBox.set(id, {
          mainStart: mainCursor,
          mainEnd: mainCursor + plan.mainSize,
          crossStart,
          crossMid: crossStart + fullCross / 2,
        });
        for (const m of membersByGroup.get(id) ?? []) {
          const rel = plan.memberPos.get(m.id)!;
          const relCross = rel.cross + GROUP_HEADER;
          const relPos = proj(rel.main, relCross);
          const b = BOX[m.kind];
          nodes.push({
            id: m.id,
            type:
              m.kind === "input"
                ? "graphInput"
                : m.kind === "airflow"
                  ? "graphAirflow"
                  : "graphEvent",
            parentId: id,
            extent: "parent",
            draggable: false,
            position: relPos,
            style: { width: b.w, height: b.h },
            data: {
              kind: m.kind,
              label: m.label,
              detail: m.detail,
              isBranch: m.is_branch,
              required: m.required,
              orientation,
            },
          });
          absBox.set(m.id, {
            mainStart: mainCursor + rel.main,
            mainEnd: mainCursor + rel.main + mainExtent(m, isLR),
            crossStart: crossStart + relCross,
            crossMid:
              crossStart + relCross + crossExtent(m, isLR) / 2,
          });
        }
      } else {
        const wb = wbNodes.find((n) => n.id === id)!;
        const pos = proj(mainCursor, crossStart);
        const b = BOX.workflow_backend;
        nodes.push({
          id,
          type: "graphEvent",
          position: pos,
          style: { width: b.w, height: b.h },
          data: {
            kind: "workflow_backend",
            label: wb.label,
            detail: wb.detail,
            isBranch: wb.is_branch,
            required: false,
            orientation,
          },
        });
        absBox.set(id, {
          mainStart: mainCursor,
          mainEnd: mainCursor + plan.mainSize,
          crossStart,
          crossMid: crossStart + plan.crossSize / 2,
        });
      }
      crossCursor += fullCross + SIBLING_GAP;
    }
    mainCursor += colMain + LANE_GAP;
  }

  // --- Route the edges ---
  const ringBase =
    Math.min(...columnOrder.map((id) => colCrossStart.get(id)!)) -
    RING_OFFSET;

  let ringCount = 0;
  const gutterUse = new Map<string, number>();
  const laneCount = (key: string): number => {
    const n = gutterUse.get(key) ?? 0;
    gutterUse.set(key, n + 1);
    return n;
  };

  // Node group + rank, for deciding direct vs ring routing.
  const nodeGroup = new Map<string, string | null>();
  const nodeRank = new Map<string, number>();
  for (const n of graph.nodes) {
    nodeGroup.set(n.id, n.group_id);
    nodeRank.set(n.id, n.rank);
  }

  const edges: Edge[] = [];
  for (const e of graph.edges) {
    const sb = absBox.get(e.from_node);
    const tb = absBox.get(e.to_node);
    if (!sb || !tb) continue;
    const functional = e.dep_kind !== "display";

    // An edge bends once (direct) when it's the execution flow between
    // adjacent columns, or links adjacent ranks inside one group —
    // there's a clear gap to bend in. Anything that skips a column or
    // rank routes out over the ring.
    const sg = nodeGroup.get(e.from_node);
    const tg = nodeGroup.get(e.to_node);
    const sr = nodeRank.get(e.from_node);
    const tr = nodeRank.get(e.to_node);
    const adjacentInGroup =
      sg != null &&
      sg === tg &&
      sr !== undefined &&
      tr !== undefined &&
      tr === sr + 1;
    const direct = e.relation === "execution" || adjacentInGroup;

    let route: EdgeRoute;
    if (!direct) {
      // Out through the source's exit lane, across the ring, back down.
      const lane = ringCount++;
      route = {
        orientation,
        kind: "ring",
        mid: 0,
        lane1: sb.mainEnd + EXIT_OFF,
        ring: ringBase - lane * RING_STEP,
        lane2: tb.mainStart - EXIT_OFF,
        relation: e.relation,
        functional,
      };
    } else {
      // A single bend in the gap between the two columns / ranks.
      const gapMid = (sb.mainEnd + tb.mainStart) / 2;
      const key = `${e.relation}:${e.to_node}`;
      const lane = laneCount(key);
      const spread = e.relation === "in_group" ? 4 : 10;
      route = {
        orientation,
        kind: "direct",
        mid: gapMid + (lane - 1) * spread,
        lane1: 0,
        ring: 0,
        lane2: 0,
        relation: e.relation,
        functional,
      };
    }
    edges.push({
      id: e.id,
      source: e.from_node,
      target: e.to_node,
      type: "orthogonal",
      data: { route, dimmed: false, highlighted: false },
    });
  }

  return { nodes, edges };
}
