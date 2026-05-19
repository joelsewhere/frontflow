import { type EdgeProps } from "@xyflow/react";
import { type GraphEdgeData, type EdgeRoute } from "./layout";

type Pt = [number, number];

/**
 * The ordered corner points of an edge's orthogonal route. Endpoint
 * coordinates come live from React Flow; the interior corners come
 * from the precomputed route — rank-gap / gutter lanes and the ring —
 * so every elbow lands in clear space.
 */
function routePoints(
  route: EdgeRoute,
  sx: number,
  sy: number,
  tx: number,
  ty: number,
): Pt[] {
  const lr = route.orientation === "LR";
  let pts: Pt[];
  if (route.kind === "direct") {
    pts = lr
      ? [
          [sx, sy],
          [route.mid, sy],
          [route.mid, ty],
          [tx, ty],
        ]
      : [
          [sx, sy],
          [sx, route.mid],
          [tx, route.mid],
          [tx, ty],
        ];
  } else {
    pts = lr
      ? [
          [sx, sy],
          [route.lane1, sy],
          [route.lane1, route.ring],
          [route.lane2, route.ring],
          [route.lane2, ty],
          [tx, ty],
        ]
      : [
          [sx, sy],
          [sx, route.lane1],
          [route.ring, route.lane1],
          [route.ring, route.lane2],
          [tx, route.lane2],
          [tx, ty],
        ];
  }
  const out: Pt[] = [];
  for (const p of pts) {
    const last = out[out.length - 1];
    if (
      !last ||
      Math.abs(last[0] - p[0]) > 0.5 ||
      Math.abs(last[1] - p[1]) > 0.5
    ) {
      out.push(p);
    }
  }
  return out;
}

function toPath(pts: Pt[]): string {
  return pts
    .map((p, i) => `${i === 0 ? "M" : "L"} ${p[0]} ${p[1]}`)
    .join(" ");
}

function arrowPoints(orientation: "LR" | "TB", tx: number, ty: number): string {
  const s = 6;
  return orientation === "LR"
    ? `${tx},${ty} ${tx - s},${ty - s * 0.6} ${tx - s},${ty + s * 0.6}`
    : `${tx},${ty} ${tx - s * 0.6},${ty - s} ${tx + s * 0.6},${ty - s}`;
}

/** Base stroke for each edge relation. */
function edgeStyle(route: EdgeRoute): {
  stroke: string;
  width: number;
  dash?: string;
  opacity: number;
} {
  if (route.relation === "in_group") {
    return {
      stroke: "rgb(var(--color-muted))",
      width: 1,
      opacity: 0.5,
    };
  }
  if (route.relation === "execution") {
    return {
      stroke: "rgb(var(--color-ink))",
      width: 1.4,
      opacity: 0.45,
    };
  }
  // dependency
  return {
    stroke: "rgb(var(--color-accent))",
    width: route.functional ? 1.7 : 1.3,
    dash: route.functional ? undefined : "5 3",
    opacity: route.functional ? 0.9 : 0.62,
  };
}

/**
 * A graph edge as an orthogonal path. In-group flow and the execution
 * spine are quiet neutral lines; dependency edges are accent-coloured
 * (functional solid, display dashed). Hover state dims edges outside
 * the hovered subgraph and emphasizes those inside it.
 */
export function OrthogonalEdge({
  sourceX,
  sourceY,
  targetX,
  targetY,
  data,
}: EdgeProps) {
  const { route, dimmed, highlighted } = data as GraphEdgeData;
  const pts = routePoints(route, sourceX, sourceY, targetX, targetY);
  const d = toPath(pts);
  const base = edgeStyle(route);

  const width = highlighted ? base.width + 1 : base.width;
  const opacity = dimmed ? 0.08 : highlighted ? 1 : base.opacity;

  return (
    <g opacity={opacity}>
      <path
        d={d}
        fill="none"
        stroke={base.stroke}
        strokeWidth={width}
        strokeDasharray={base.dash}
        className="react-flow__edge-path"
      />
      <polygon
        points={arrowPoints(route.orientation, targetX, targetY)}
        fill={base.stroke}
      />
    </g>
  );
}

export const graphEdgeTypes = {
  orthogonal: OrthogonalEdge,
};
