import { useMemo, useRef, useState } from "react";
import type {
  SubmissionRateInterval,
  SubmissionRateResponse,
} from "../../lib/api";

interface SubmissionRateChartProps {
  data: SubmissionRateResponse;
  /** Controlled interval — null = auto (server picked). */
  selectedInterval: SubmissionRateInterval | null;
  onIntervalChange: (i: SubmissionRateInterval | null) => void;
}

const HEIGHT = 200;
const PAD_TOP = 16;
const PAD_BOTTOM = 28;
const PAD_LEFT = 36;
const PAD_RIGHT = 12;

const INTERVAL_OPTIONS: SubmissionRateInterval[] = [
  "minute",
  "5min",
  "15min",
  "hour",
  "day",
  "week",
  "month",
];

const INTERVAL_LABEL: Record<SubmissionRateInterval, string> = {
  minute: "1m",
  "5min": "5m",
  "15min": "15m",
  hour: "1h",
  day: "1d",
  week: "1w",
  month: "1mo",
};

/** Line chart of submissions per fine-grained time bucket. Built for
 *  activity / security monitoring — spotting volume spikes that
 *  daily-bucket throughput would smear into the baseline. Single
 *  line (no state breakdown) for the same reason: color stacking
 *  would distribute a spike across the stack and bury it.
 *
 *  Hover tooltip pattern matches the other charts in this page
 *  (mouse-tracking overlay positioned relative to the wrapping
 *  div). Tooltip surfaces the exact count for the hovered bucket
 *  and its time. */
export function SubmissionRateChart({
  data,
  selectedInterval,
  onIntervalChange,
}: SubmissionRateChartProps) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [hover, setHover] = useState<
    { idx: number; x: number; y: number } | null
  >(null);

  const layout = useMemo(() => {
    if (data.buckets.length === 0) return null;
    const width = 600;
    const chartW = width - PAD_LEFT - PAD_RIGHT;
    const chartH = HEIGHT - PAD_TOP - PAD_BOTTOM;
    // Y axis goes from 0 to peak (with a small headroom of +1 so the
    // peak bucket isn't pinned to the very top edge).
    const maxY = Math.max(1, data.peak_count + 1);
    const xStep =
      data.buckets.length <= 1 ? 0 : chartW / (data.buckets.length - 1);
    const points = data.buckets.map((b, i) => ({
      x: PAD_LEFT + i * xStep,
      y: PAD_TOP + chartH - (b.count / maxY) * chartH,
      bucket: b,
      idx: i,
    }));
    return { width, chartW, chartH, maxY, xStep, points };
  }, [data]);

  return (
    <div>
      {/* Interval picker — same pattern as the throughput chart's
          control. Null override = "use whatever the server auto-
          picked from the date range." */}
      <div className="mb-3 flex items-center gap-2 flex-wrap">
        <span className="font-sans text-xs uppercase tracking-[0.12em] text-muted">
          Interval:
        </span>
        {INTERVAL_OPTIONS.map((i) => {
          const isActive = selectedInterval
            ? selectedInterval === i
            : data.interval === i;
          return (
            <button
              key={i}
              type="button"
              onClick={() =>
                onIntervalChange(selectedInterval === i ? null : i)
              }
              className={`font-mono text-xs px-2 py-0.5 border ${
                isActive
                  ? "border-ink bg-ink text-bg"
                  : "border-border text-muted hover:text-ink"
              }`}
            >
              {INTERVAL_LABEL[i]}
            </button>
          );
        })}
        {selectedInterval ? (
          <span className="font-sans text-xs text-muted">(override)</span>
        ) : (
          <span className="font-sans text-xs text-muted">(auto)</span>
        )}
      </div>
      {/* Summary stats — peak and mean are the at-a-glance numbers
          for "is anything unusual." */}
      <div className="mb-3 flex gap-6 text-xs text-muted font-mono">
        <span>
          peak <span className="text-ink">{data.peak_count.toLocaleString()}</span>
          {data.peak_start ? (
            <span className="ml-1">
              @ {formatBucketTime(data.peak_start, data.interval)}
            </span>
          ) : null}
        </span>
        <span>
          mean <span className="text-ink">{data.mean_count.toFixed(1)}</span> per bucket
        </span>
        <span>
          total <span className="text-ink">{data.total.toLocaleString()}</span>
        </span>
      </div>
      {layout ? (
        <div ref={wrapRef} className="relative">
          <svg
            viewBox={`0 0 ${layout.width} ${HEIGHT}`}
            className="w-full"
            style={{ maxHeight: HEIGHT }}
            onPointerLeave={() => setHover(null)}
            onPointerMove={(e) => {
              if (layout.points.length === 0) return;
              const svg = e.currentTarget as SVGSVGElement;
              const r = wrapRef.current?.getBoundingClientRect();
              if (!r) return;
              // Map the mouse position into viewBox coordinates by
              // running the SVG's screen CTM in reverse. This is the
              // robust way to handle whatever preserveAspectRatio
              // padding the browser added (default `xMidYMid meet`
              // can leave significant horizontal gutters when the
              // container is wider than the viewBox aspect ratio).
              const pt = svg.createSVGPoint();
              pt.x = e.clientX;
              pt.y = e.clientY;
              const ctm = svg.getScreenCTM();
              if (!ctm) return;
              const vbPt = pt.matrixTransform(ctm.inverse());
              const relativeX = vbPt.x - PAD_LEFT;
              const idx =
                layout.xStep > 0
                  ? Math.max(
                      0,
                      Math.min(
                        layout.points.length - 1,
                        Math.round(relativeX / layout.xStep),
                      ),
                    )
                  : 0;
              // Convert the snapped bucket's viewBox coords back to
              // client/wrap-div coords for the HTML tooltip anchor.
              // Use the same CTM (forward this time) so the tooltip
              // lands exactly on the drawn marker regardless of the
              // SVG's aspect-ratio padding.
              const snappedVb = svg.createSVGPoint();
              snappedVb.x = layout.points[idx].x;
              snappedVb.y = layout.points[idx].y;
              const snappedScreen = snappedVb.matrixTransform(ctm);
              setHover({
                idx,
                x: snappedScreen.x - r.left,
                y: snappedScreen.y - r.top,
              });
            }}
          >
            {/* Y-axis ticks — 0 at baseline, maxY at top */}
            <text
              x={PAD_LEFT - 4}
              y={PAD_TOP + 6}
              textAnchor="end"
              fontSize={9}
              fill="rgb(var(--color-muted))"
              fontFamily="var(--font-mono)"
            >
              {layout.maxY - 1}
            </text>
            <text
              x={PAD_LEFT - 4}
              y={PAD_TOP + layout.chartH}
              textAnchor="end"
              fontSize={9}
              fill="rgb(var(--color-muted))"
              fontFamily="var(--font-mono)"
            >
              0
            </text>
            {/* The line itself — single polyline through all points. */}
            <polyline
              points={layout.points.map((p) => `${p.x},${p.y}`).join(" ")}
              fill="none"
              stroke="rgb(var(--color-ink))"
              strokeWidth={1.5}
            />
            {/* Hover marker on the focused bucket */}
            {hover ? (
              <>
                <line
                  x1={layout.points[hover.idx].x}
                  x2={layout.points[hover.idx].x}
                  y1={PAD_TOP}
                  y2={PAD_TOP + layout.chartH}
                  stroke="rgb(var(--color-muted))"
                  strokeWidth={0.5}
                  opacity={0.6}
                />
                <circle
                  cx={layout.points[hover.idx].x}
                  cy={layout.points[hover.idx].y}
                  r={3.5}
                  fill="rgb(var(--color-ink))"
                />
              </>
            ) : null}
            {/* X-axis ticks — first, middle, last */}
            {[
              0,
              Math.floor(layout.points.length / 2),
              layout.points.length - 1,
            ]
              .filter((v, i, a) => a.indexOf(v) === i)
              .map((i) => {
                const p = layout.points[i];
                return (
                  <text
                    key={`xt-${i}`}
                    x={p.x}
                    y={HEIGHT - 8}
                    textAnchor={
                      i === 0
                        ? "start"
                        : i === layout.points.length - 1
                          ? "end"
                          : "middle"
                    }
                    fontSize={9}
                    fill="rgb(var(--color-muted))"
                    fontFamily="var(--font-mono)"
                  >
                    {formatBucketTime(p.bucket.start, data.interval)}
                  </text>
                );
              })}
          </svg>
          {hover ? (
            <div
              className="absolute pointer-events-none bg-ink text-bg font-mono text-[11px] px-2 py-1 whitespace-nowrap shadow-sm z-10"
              style={{
                left: `${hover.x}px`,
                top: `${hover.y}px`,
                transform: "translate(12px, 12px)",
              }}
            >
              <div className="font-sans uppercase tracking-[0.12em]">
                {formatBucketTime(
                  layout.points[hover.idx].bucket.start,
                  data.interval,
                  /*long=*/ true,
                )}
              </div>
              <div className="opacity-70 mt-0.5">
                {layout.points[hover.idx].bucket.count.toLocaleString()}{" "}
                submission
                {layout.points[hover.idx].bucket.count === 1 ? "" : "s"}
              </div>
            </div>
          ) : null}
        </div>
      ) : (
        <p className="text-xs text-muted">No submissions in this range.</p>
      )}
    </div>
  );
}

/** Format a bucket's start time for axis/tooltip display. The
 *  resolution shown depends on the bucket size — minute buckets
 *  show HH:MM, day buckets show MMM DD, etc. `long` mode includes
 *  more context (used in the tooltip where space allows). */
function formatBucketTime(
  iso: string,
  interval: SubmissionRateInterval,
  long: boolean = false,
): string {
  const d = new Date(iso);
  if (interval === "minute" || interval === "5min" || interval === "15min") {
    return long
      ? d.toLocaleString(undefined, {
          month: "short",
          day: "numeric",
          hour: "2-digit",
          minute: "2-digit",
        })
      : d.toLocaleTimeString(undefined, {
          hour: "2-digit",
          minute: "2-digit",
        });
  }
  if (interval === "hour") {
    return long
      ? d.toLocaleString(undefined, {
          month: "short",
          day: "numeric",
          hour: "2-digit",
        })
      : d.toLocaleString(undefined, {
          month: "short",
          day: "numeric",
          hour: "2-digit",
        });
  }
  if (interval === "month") {
    return d.toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
    });
  }
  // day / week
  return d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}
