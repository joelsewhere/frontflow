import { useMemo, useRef, useState } from "react";
import type { FlowEdge, FlowNode, FlowResponse } from "../../lib/api";

/** Submission-flow sankey: a stacked-node Sankey diagram showing
 *  how submissions move through the form. Each node is a rectangle
 *  whose total height is proportional to its reach count. Inside
 *  the rectangle, vertical sub-stacks one per outgoing edge
 *  (neutral grey, continuing) plus one each for failed (red) and
 *  in_flight (accent) terminals. Each continuing sub-slice emits
 *  an SVG ribbon to its target node at exactly its own height —
 *  so an edge's width directly represents the count it carries.
 *
 *  Layout is column-based: nodes are placed in columns by their
 *  longest path from any root (node with no incoming edges).
 *  Within a column, nodes stack vertically by registration order
 *  (the order they appear in the flow response, which matches the
 *  form-author's declaration order).
 *
 *  Node click is exposed via `onNodeClick` for toggle-filtering
 *  the rest of the analytics page (per the established UX pattern).
 *  Edge click is not exposed in this slice — it's a fiddly UX
 *  problem (thin SVG paths are awkward targets) that we deferred
 *  in design. Hover tooltips on both nodes and edges surface exact
 *  counts. */
interface FlowSankeyProps {
  data: FlowResponse;
  /** Optional click handler — when set, nodes become buttons that
   *  invoke this with their node_id. */
  onNodeClick?: (nodeId: string) => void;
  /** Optional active-key set — node_ids in this list render with
   *  full opacity, others render greyed. Matches the bar-chart
   *  "filter selection narrows other bars but keeps them visible"
   *  pattern. When undefined, every node renders at full opacity. */
  activeKeys?: string[];
}

// Layout constants. Tuned for readability at ~800px wide; the SVG
// scales via viewBox so larger containers just zoom.
const NODE_WIDTH = 14;
const COLUMN_GAP = 140;
const NODE_GAP = 16;
const PAD_LEFT = 80;
const PAD_RIGHT = 80;
const PAD_TOP = 16;
const PAD_BOTTOM = 16;
// Pixel scale per submission. At low submission counts (typical
// during development and early use), a generous scale keeps slices
// visible. We don't shrink below this; we just let the SVG grow
// taller for high-count cases, and the viewBox-driven scaling
// handles container fit.
const MIN_PX_PER_SUBMISSION = 14;
// Cap so very-high-count forms don't blow out the viewBox height.
const MAX_COLUMN_PX = 480;

interface PositionedNode extends FlowNode {
  column: number;
  /** Vertical position of this node within the SVG (top edge in px). */
  y: number;
  /** Pixel height of this node — proportional to `reach`. */
  height: number;
  /** Top edge x coordinate (computed from column). */
  x: number;
  /** Within the node, the absolute y coordinates of each outgoing
   *  edge's sub-slice (top, bottom). Edges read these to know
   *  where on the source node to start the ribbon. Terminal sub-
   *  slices live in `terminalRects` and don't emit edges. */
  outgoingSlices: { target: string; count: number; top: number; bottom: number }[];
  /** Terminal sub-slice rectangles (failed / in_flight / succeeded).
   *  Rendered inside the node as colored bands. */
  terminalRects: { kind: "failed" | "in_flight" | "succeeded"; count: number; top: number; bottom: number }[];
}

/** Compute the longest-path-from-root column index for every node.
 *  A node with no incoming edges is column 0. A node's column is
 *  `1 + max(column of any predecessor)`. Handles disconnected nodes
 *  (defaults to column 0) and is acyclic-by-assumption (form graphs
 *  are DAGs at the node level — branches diverge then re-converge
 *  rarely, and never form cycles). */
function computeColumns(nodes: FlowNode[], edges: FlowEdge[]): Map<string, number> {
  const incoming: Map<string, string[]> = new Map();
  for (const n of nodes) incoming.set(n.node_id, []);
  for (const e of edges) {
    if (!incoming.has(e.target)) incoming.set(e.target, []);
    incoming.get(e.target)!.push(e.source);
  }
  const cols: Map<string, number> = new Map();
  // Memoized longest-path computation. Defensive against cycles —
  // if we revisit a node mid-computation we bail with what we have.
  const visiting: Set<string> = new Set();
  function colOf(id: string): number {
    if (cols.has(id)) return cols.get(id)!;
    if (visiting.has(id)) return 0;
    visiting.add(id);
    const preds = incoming.get(id) ?? [];
    const c = preds.length === 0 ? 0 : 1 + Math.max(...preds.map(colOf));
    visiting.delete(id);
    cols.set(id, c);
    return c;
  }
  for (const n of nodes) colOf(n.node_id);
  return cols;
}

/** Build the full positioned layout: column assignment, vertical
 *  stacking within columns by registration order, per-node height
 *  proportional to reach, sub-slice positions for outgoing edges
 *  and terminals. */
function layout(data: FlowResponse): {
  positioned: PositionedNode[];
  width: number;
  height: number;
} {
  const cols = computeColumns(data.nodes, data.edges);
  const maxCol = Math.max(0, ...Array.from(cols.values()));

  // Group nodes by column. Within a column, preserve the order they
  // appear in `data.nodes` (the form-author's declaration order).
  const byColumn: FlowNode[][] = [];
  for (let i = 0; i <= maxCol; i++) byColumn.push([]);
  for (const n of data.nodes) {
    byColumn[cols.get(n.node_id) ?? 0].push(n);
  }

  // Pixel scale per submission. Start generous (so low-N cases
  // read well) and only shrink if a column's total would exceed
  // the max column height. Picking the largest column once means
  // every column uses the same scale and they're visually
  // comparable.
  let pxPerSubmission = MIN_PX_PER_SUBMISSION;
  for (const col of byColumn) {
    const totalReach = col.reduce((s, n) => s + n.reach, 0);
    const gaps = (col.length - 1) * NODE_GAP;
    if (totalReach > 0) {
      const wouldBe = totalReach * pxPerSubmission + gaps;
      if (wouldBe > MAX_COLUMN_PX) {
        pxPerSubmission = Math.max(
          2,
          (MAX_COLUMN_PX - gaps) / totalReach,
        );
      }
    }
  }

  const positioned: PositionedNode[] = [];
  for (let c = 0; c <= maxCol; c++) {
    const col = byColumn[c];
    let y = PAD_TOP;
    for (const n of col) {
      const x = PAD_LEFT + c * COLUMN_GAP;
      // Compute sub-slices. The node's vertical space is divided
      // proportionally by sub-slice count — outgoing edges by their
      // own counts, terminals by theirs. They share the same px
      // scale (pxPerSubmission) as the node itself, so a slice's
      // pixel height is its count × scale.
      const outgoing = data.edges.filter((e) => e.source === n.node_id);
      const failed = n.terminals.failed ?? 0;
      const inFlight = n.terminals.in_flight ?? 0;
      const succeeded = n.terminals.succeeded ?? 0;

      const outgoingSlices: PositionedNode["outgoingSlices"] = [];
      const terminalRects: PositionedNode["terminalRects"] = [];

      // Stack order: outgoing edges first (in source-edge order),
      // then succeeded, then in_flight, then failed. Succeeded
      // before in-flight before failed groups the "good fates"
      // toward the top of the node and "needs attention" toward
      // the bottom, which is consistent with how the bar charts
      // sort severity.
      let sliceTop = y;
      for (const e of outgoing) {
        const sliceHeight = e.count * pxPerSubmission;
        if (sliceHeight > 0) {
          outgoingSlices.push({
            target: e.target,
            count: e.count,
            top: sliceTop,
            bottom: sliceTop + sliceHeight,
          });
          sliceTop += sliceHeight;
        } else {
          // Zero-count outgoing edges still appear in the response
          // (stable axis); they emit a zero-width ribbon, which
          // collapses visually but is still in the data.
          outgoingSlices.push({
            target: e.target,
            count: 0,
            top: sliceTop,
            bottom: sliceTop,
          });
        }
      }
      if (succeeded > 0) {
        const sliceHeight = succeeded * pxPerSubmission;
        terminalRects.push({
          kind: "succeeded",
          count: succeeded,
          top: sliceTop,
          bottom: sliceTop + sliceHeight,
        });
        sliceTop += sliceHeight;
      }
      if (inFlight > 0) {
        const sliceHeight = inFlight * pxPerSubmission;
        terminalRects.push({
          kind: "in_flight",
          count: inFlight,
          top: sliceTop,
          bottom: sliceTop + sliceHeight,
        });
        sliceTop += sliceHeight;
      }
      if (failed > 0) {
        const sliceHeight = failed * pxPerSubmission;
        terminalRects.push({
          kind: "failed",
          count: failed,
          top: sliceTop,
          bottom: sliceTop + sliceHeight,
        });
        sliceTop += sliceHeight;
      }
      // Frame height = exact sub-slice extent. No minimum; if a
      // node has reach=0 the frame collapses to height 0 and only
      // the label remains. This preserves flow conservation
      // visually — frame height is always (reach × pxPerSubmission).
      const usedHeight = sliceTop - y;

      positioned.push({
        ...n,
        column: c,
        x,
        y,
        height: usedHeight,
        outgoingSlices,
        terminalRects,
      });

      y += Math.max(usedHeight, 2) + NODE_GAP;
    }
  }

  const width =
    PAD_LEFT + PAD_RIGHT + (maxCol + 1) * COLUMN_GAP - (COLUMN_GAP - NODE_WIDTH);
  const height =
    PAD_TOP +
    PAD_BOTTOM +
    Math.max(
      80,
      ...byColumn.map((col) => {
        const totalReach = col.reduce((s, n) => s + n.reach, 0);
        return totalReach * pxPerSubmission +
          Math.max(0, col.length - 1) * NODE_GAP;
      }),
    );

  return { positioned, width, height };
}

/** Build the SVG filled-ribbon path for one edge. Connects the
 *  source sub-slice's vertical extent to the target node's
 *  corresponding incoming slice via two cubic Bezier curves —
 *  one for the top edge, one for the bottom — forming a closed
 *  filled shape. */
function ribbonPath(
  sx: number,
  sTop: number,
  sBottom: number,
  tx: number,
  tTop: number,
  tBottom: number,
): string {
  const mx = (sx + tx) / 2;
  // Top curve: source-top → target-top.
  // Bottom curve: target-bottom → source-bottom (reverse direction).
  return [
    `M ${sx} ${sTop}`,
    `C ${mx} ${sTop} ${mx} ${tTop} ${tx} ${tTop}`,
    `L ${tx} ${tBottom}`,
    `C ${mx} ${tBottom} ${mx} ${sBottom} ${sx} ${sBottom}`,
    `Z`,
  ].join(" ");
}

export function FlowSankey({
  data,
  onNodeClick,
  activeKeys,
}: FlowSankeyProps) {
  const { positioned, width, height } = useMemo(
    () => layout(data),
    [data],
  );
  const byId = useMemo(() => {
    const m: Map<string, PositionedNode> = new Map();
    for (const p of positioned) m.set(p.node_id, p);
    return m;
  }, [positioned]);

  // Tooltip state — what's hovered and where on the wrapper to
  // anchor the popup. Position is page-relative-to-wrapper (not
  // SVG-viewBox-relative), so we capture the mouse coords from
  // clientX/clientY and subtract the wrapper's bounding rect.
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [hover, setHover] = useState<
    | null
    | { kind: "node"; node: PositionedNode; x: number; y: number }
    | {
        kind: "slice";
        node: PositionedNode;
        sliceKind: "outgoing" | "succeeded" | "in_flight" | "failed";
        target?: string;
        count: number;
        x: number;
        y: number;
      }
    | { kind: "edge"; edge: FlowEdge; x: number; y: number }
  >(null);

  function track(e: React.PointerEvent): { x: number; y: number } {
    const rect = wrapRef.current?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }

  // Compute incoming-edge target slots per node. Per the design,
  // incoming edges merge into the node total without sub-stacking —
  // we just spread them evenly across the target node's left edge
  // in the order they appear in `data.edges`. Each ribbon's target
  // height is the same as the source slice's height (so widths
  // match end-to-end). Distribution is by proportional position
  // within the node — first incoming edge owns the top section,
  // next owns the section below, etc.
  type EdgeRibbon = {
    edge: FlowEdge;
    sx: number;
    sTop: number;
    sBottom: number;
    tx: number;
    tTop: number;
    tBottom: number;
  };
  const ribbons: EdgeRibbon[] = useMemo(() => {
    // For each target node, accumulate a running y offset for
    // incoming edges. Same pxPerSubmission scale is implicit — the
    // source sub-slice has height proportional to its count; we
    // claim the same number of pixels at the target.
    const targetCursor: Map<string, number> = new Map();
    const out: EdgeRibbon[] = [];
    for (const src of positioned) {
      for (const slice of src.outgoingSlices) {
        if (slice.count === 0) continue;
        const tgt = byId.get(slice.target);
        if (!tgt) continue;
        const cursor = targetCursor.get(tgt.node_id) ?? tgt.y;
        const ribbonHeight = slice.bottom - slice.top;
        const tTop = cursor;
        const tBottom = Math.min(tgt.y + tgt.height, cursor + ribbonHeight);
        targetCursor.set(tgt.node_id, tBottom);
        out.push({
          edge: { source: src.node_id, target: slice.target, count: slice.count },
          sx: src.x + NODE_WIDTH,
          sTop: slice.top,
          sBottom: slice.bottom,
          tx: tgt.x,
          tTop,
          tBottom,
        });
      }
    }
    return out;
  }, [positioned, byId]);

  if (positioned.length === 0) {
    return <p className="text-xs text-muted">No flow data.</p>;
  }

  return (
    <div className="overflow-x-auto">
      <div ref={wrapRef} className="relative">
        <svg
          viewBox={`0 0 ${width} ${height}`}
          className="w-full"
          style={{ minWidth: width, maxHeight: 480 }}
          onPointerLeave={() => setHover(null)}
        >
        {/* Edges first — they render behind the nodes. Each is a
            filled grey ribbon at 25% opacity. Hover surfaces the
            source → target count via the overlay tooltip. */}
        {ribbons.map((r, i) => (
          <path
            key={`r-${i}`}
            d={ribbonPath(r.sx, r.sTop, r.sBottom, r.tx, r.tTop, r.tBottom)}
            fill="rgb(var(--color-muted))"
            fillOpacity={0.25}
            onPointerEnter={(e) =>
              setHover({ kind: "edge", edge: r.edge, ...track(e) })
            }
            onPointerMove={(e) =>
              setHover((h) =>
                h && h.kind === "edge" && h.edge === r.edge
                  ? { ...h, ...track(e) }
                  : h,
              )
            }
          />
        ))}

        {/* Nodes — outer rectangle frame (transparent fill), then
            colored sub-slices for outgoing/terminal bands inside.
            The outer frame uses the ink color so the node is
            visually grouped even when its sub-slices are pale. */}
        {positioned.map((n) => {
          const isActive =
            !activeKeys || activeKeys.length === 0 ||
            activeKeys.includes(n.node_id);
          const opacity = isActive ? 1 : 0.35;
          return (
            <g
              key={n.node_id}
              opacity={opacity}
              style={{ cursor: onNodeClick ? "pointer" : "default" }}
              onClick={
                onNodeClick ? () => onNodeClick(n.node_id) : undefined
              }
              onPointerEnter={(e) =>
                setHover({ kind: "node", node: n, ...track(e) })
              }
              onPointerMove={(e) =>
                setHover((h) =>
                  h && h.kind === "node" && h.node.node_id === n.node_id
                    ? { ...h, ...track(e) }
                    : h,
                )
              }
            >
              {/* Outgoing continuing slices — neutral grey, full
                  intensity so they read as "the path." */}
              {n.outgoingSlices.map((s, i) =>
                s.bottom - s.top > 0 ? (
                  <rect
                    key={`os-${i}`}
                    x={n.x}
                    y={s.top}
                    width={NODE_WIDTH}
                    height={s.bottom - s.top}
                    fill="rgb(var(--color-muted))"
                    fillOpacity={0.6}
                    onPointerEnter={(e) => {
                      e.stopPropagation();
                      setHover({
                        kind: "slice",
                        node: n,
                        sliceKind: "outgoing",
                        target: s.target,
                        count: s.count,
                        ...track(e),
                      });
                    }}
                    onPointerMove={(e) =>
                      setHover((h) =>
                        h && h.kind === "slice" &&
                        h.sliceKind === "outgoing" &&
                        h.node.node_id === n.node_id &&
                        h.target === s.target
                          ? { ...h, ...track(e) }
                          : h,
                      )
                    }
                  />
                ) : null,
              )}
              {/* Terminal sub-slices — distinct colors per kind.
                  succeeded = ink (solid, "done well"); in_flight =
                  accent (muted blue, "moving"); failed = error
                  (red, "needs attention"). */}
              {n.terminalRects.map((t, i) => {
                const fill =
                  t.kind === "failed"
                    ? "rgb(var(--color-error))"
                    : t.kind === "in_flight"
                      ? "rgb(var(--color-accent))"
                      : "rgb(var(--color-ink))";
                return (
                  <rect
                    key={`t-${i}`}
                    x={n.x}
                    y={t.top}
                    width={NODE_WIDTH}
                    height={t.bottom - t.top}
                    fill={fill}
                    fillOpacity={0.85}
                    onPointerEnter={(e) => {
                      e.stopPropagation();
                      setHover({
                        kind: "slice",
                        node: n,
                        sliceKind: t.kind,
                        count: t.count,
                        ...track(e),
                      });
                    }}
                    onPointerMove={(e) =>
                      setHover((h) =>
                        h && h.kind === "slice" &&
                        h.sliceKind === t.kind &&
                        h.node.node_id === n.node_id
                          ? { ...h, ...track(e) }
                          : h,
                      )
                    }
                  />
                );
              })}
              {/* Outer frame — a thin border so the node reads as
                  one unit. */}
              <rect
                x={n.x}
                y={n.y}
                width={NODE_WIDTH}
                height={n.height}
                fill="none"
                stroke="rgb(var(--color-ink))"
                strokeWidth={0.75}
              />
              {/* Label. Positioned right of the node for left-
                  column nodes; flipped to the left for terminal
                  nodes (no outgoing edges = right side is empty). */}
              {n.outgoingSlices.length === 0 ? (
                <text
                  x={n.x - 6}
                  y={n.y + n.height / 2}
                  textAnchor="end"
                  dominantBaseline="middle"
                  fontSize={11}
                  fill="rgb(var(--color-ink))"
                  fontFamily="var(--font-sans)"
                  style={{ pointerEvents: "none" }}
                >
                  {n.label}
                </text>
              ) : (
                <text
                  x={n.x + NODE_WIDTH + 6}
                  y={n.y + n.height / 2}
                  textAnchor="start"
                  dominantBaseline="middle"
                  fontSize={11}
                  fill="rgb(var(--color-ink))"
                  fontFamily="var(--font-sans)"
                  style={{ pointerEvents: "none" }}
                >
                  {n.label}
                </text>
              )}
              {/* Reach count, in mono font below the label. */}
              {n.outgoingSlices.length === 0 ? (
                <text
                  x={n.x - 6}
                  y={n.y + n.height / 2 + 14}
                  textAnchor="end"
                  dominantBaseline="middle"
                  fontSize={9}
                  fill="rgb(var(--color-muted))"
                  fontFamily="var(--font-mono)"
                  style={{ pointerEvents: "none" }}
                >
                  {n.reach}
                </text>
              ) : (
                <text
                  x={n.x + NODE_WIDTH + 6}
                  y={n.y + n.height / 2 + 14}
                  textAnchor="start"
                  dominantBaseline="middle"
                  fontSize={9}
                  fill="rgb(var(--color-muted))"
                  fontFamily="var(--font-mono)"
                  style={{ pointerEvents: "none" }}
                >
                  {n.reach}
                </text>
              )}
            </g>
          );
        })}
        </svg>
        {hover ? <FlowTooltip hover={hover} /> : null}
      </div>
      {/* Legend — three small swatches with labels, aligned to the
          chart's bottom edge. Keeps the encoding discoverable
          without forcing the user to hover. */}
      <div className="mt-3 flex flex-wrap items-center gap-4 text-xs text-muted">
        <LegendSwatch color="rgb(var(--color-muted))" opacity={0.6} label="Continuing" />
        <LegendSwatch color="rgb(var(--color-ink))" opacity={0.85} label="Succeeded" />
        <LegendSwatch color="rgb(var(--color-accent))" opacity={0.85} label="In flight" />
        <LegendSwatch color="rgb(var(--color-error))" opacity={0.85} label="Failed" />
      </div>
    </div>
  );
}

function LegendSwatch({
  color,
  opacity,
  label,
}: {
  color: string;
  opacity: number;
  label: string;
}) {
  return (
    <span className="inline-flex items-center gap-2">
      <span
        aria-hidden="true"
        className="inline-block h-3 w-3 border border-border"
        style={{ backgroundColor: color, opacity }}
      />
      {label}
    </span>
  );
}

/** Floating tooltip pinned at the mouse position relative to the
 *  sankey's wrapping div. Content varies by what's hovered: a node
 *  shows its name + total reach + any terminal fates; a sub-slice
 *  shows the specific count for that fate or outgoing edge; an
 *  edge shows the source → target transition count. Matches the
 *  bg-ink / text-bg style used by the existing chart tooltips
 *  elsewhere in the project. */
function FlowTooltip({
  hover,
}: {
  hover:
    | { kind: "node"; node: PositionedNode; x: number; y: number }
    | {
        kind: "slice";
        node: PositionedNode;
        sliceKind: "outgoing" | "succeeded" | "in_flight" | "failed";
        target?: string;
        count: number;
        x: number;
        y: number;
      }
    | { kind: "edge"; edge: FlowEdge; x: number; y: number };
}) {
  let body: React.ReactNode;
  if (hover.kind === "node") {
    const n = hover.node;
    body = (
      <>
        <div className="font-sans uppercase tracking-[0.12em]">
          {n.label}
        </div>
        <div className="opacity-70 mt-0.5">
          Reached: {n.reach.toLocaleString()}
        </div>
        {n.terminals.succeeded ? (
          <div className="opacity-70">
            Succeeded: {n.terminals.succeeded.toLocaleString()}
          </div>
        ) : null}
        {n.terminals.in_flight ? (
          <div className="opacity-70">
            In flight: {n.terminals.in_flight.toLocaleString()}
          </div>
        ) : null}
        {n.terminals.failed ? (
          <div className="opacity-70">
            Failed: {n.terminals.failed.toLocaleString()}
          </div>
        ) : null}
      </>
    );
  } else if (hover.kind === "slice") {
    const label =
      hover.sliceKind === "outgoing"
        ? `→ ${hover.target}`
        : hover.sliceKind === "succeeded"
          ? "Succeeded"
          : hover.sliceKind === "in_flight"
            ? "In flight"
            : "Failed";
    body = (
      <>
        <div className="font-sans uppercase tracking-[0.12em]">
          {hover.node.label}
        </div>
        <div className="opacity-70 mt-0.5">{label}</div>
        <div>{hover.count.toLocaleString()}</div>
      </>
    );
  } else {
    body = (
      <>
        <div className="font-sans uppercase tracking-[0.12em]">
          {hover.edge.source} → {hover.edge.target}
        </div>
        <div className="mt-0.5">{hover.edge.count.toLocaleString()}</div>
      </>
    );
  }
  return (
    <div
      className="absolute pointer-events-none bg-ink text-bg font-mono text-[11px] px-2 py-1 whitespace-nowrap shadow-sm z-10"
      style={{
        left: `${hover.x}px`,
        top: `${hover.y}px`,
        // Anchor below-and-right of the cursor. Above-cursor would
        // clip against the chart card's top edge — the sankey has
        // no headroom up there, unlike histograms.
        transform: "translate(12px, 12px)",
      }}
    >
      {body}
    </div>
  );
}
