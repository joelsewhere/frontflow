import { useMemo, useRef, useState } from "react";
import type { HistogramBucket } from "../../lib/api";

interface HistogramChartProps {
  buckets: HistogramBucket[];
  /** Pre-formatted stat caption — typically "mean A, p50 B, p90 C"
   *  shown beneath the chart. Null when there's no data. */
  caption?: string | null;
}

const HEIGHT = 180;
const PAD_TOP = 8;
const PAD_BOTTOM = 28;
const PAD_LEFT = 28;
const PAD_RIGHT = 8;

/** Vertical-bar histogram with hover tooltip. Used for both the
 *  overall completion-time histogram and per-step drill-down. The
 *  SVG is responsive (viewBox-driven) and assumes a parent width
 *  of ~600px for typical chart cards.
 *
 *  Tooltip pattern matches FlowSankey: `position: relative` wrapper
 *  with an HTML overlay positioned at mouse coords. */
export function HistogramChart({ buckets, caption }: HistogramChartProps) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [hover, setHover] = useState<
    { idx: number; x: number; y: number } | null
  >(null);

  const layout = useMemo(() => {
    if (buckets.length === 0) return null;
    const maxCount = Math.max(1, ...buckets.map((b) => b.count));
    const width = 600;
    const chartW = width - PAD_LEFT - PAD_RIGHT;
    const chartH = HEIGHT - PAD_TOP - PAD_BOTTOM;
    const bw = chartW / buckets.length;
    return {
      width,
      maxCount,
      chartW,
      chartH,
      bw,
      bars: buckets.map((b, i) => {
        const h = (b.count / maxCount) * chartH;
        return {
          x: PAD_LEFT + i * bw,
          y: PAD_TOP + (chartH - h),
          w: Math.max(1, bw - 1),
          h,
          bucket: b,
          idx: i,
        };
      }),
    };
  }, [buckets]);

  function trackXY(e: React.PointerEvent): { x: number; y: number } {
    const r = wrapRef.current?.getBoundingClientRect();
    return r ? { x: e.clientX - r.left, y: e.clientY - r.top } : { x: 0, y: 0 };
  }

  if (!layout) {
    return <p className="text-xs text-muted">No timing data.</p>;
  }

  // Pick a few tick positions for the x-axis — first, middle, last
  // bucket labels. Showing every label gets crowded for 20 buckets.
  const tickIndices = layout.bars.length <= 5
    ? layout.bars.map((_, i) => i)
    : [0, Math.floor(layout.bars.length / 2), layout.bars.length - 1];

  return (
    <div>
      <div ref={wrapRef} className="relative">
        <svg
          viewBox={`0 0 ${layout.width} ${HEIGHT}`}
          className="w-full"
          style={{ maxHeight: HEIGHT }}
          onPointerLeave={() => setHover(null)}
        >
          {/* Y-axis ticks — just min (0) and max (count) labels. */}
          <text
            x={PAD_LEFT - 4}
            y={PAD_TOP + 8}
            textAnchor="end"
            fontSize={9}
            fill="rgb(var(--color-muted))"
            fontFamily="var(--font-mono)"
          >
            {layout.maxCount}
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
          {/* Bars */}
          {layout.bars.map((b) => (
            <rect
              key={b.idx}
              x={b.x}
              y={b.y}
              width={b.w}
              height={b.h}
              fill="rgb(var(--color-ink))"
              fillOpacity={hover?.idx === b.idx ? 1 : 0.75}
              onPointerEnter={(e) => setHover({ idx: b.idx, ...trackXY(e) })}
              onPointerMove={(e) =>
                setHover((h) =>
                  h && h.idx === b.idx ? { ...h, ...trackXY(e) } : h,
                )
              }
            />
          ))}
          {/* X-axis tick labels */}
          {tickIndices.map((i) => {
            const b = layout.bars[i];
            return (
              <text
                key={`xt-${i}`}
                x={b.x + b.w / 2}
                y={HEIGHT - 8}
                textAnchor="middle"
                fontSize={9}
                fill="rgb(var(--color-muted))"
                fontFamily="var(--font-mono)"
              >
                {b.bucket.label.split("–")[0]}
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
              {layout.bars[hover.idx].bucket.label}
            </div>
            <div className="opacity-70 mt-0.5">
              {layout.bars[hover.idx].bucket.count.toLocaleString()}{" "}
              submissions
            </div>
          </div>
        ) : null}
      </div>
      {caption ? (
        <p className="mt-2 text-xs text-muted font-mono">{caption}</p>
      ) : null}
    </div>
  );
}
