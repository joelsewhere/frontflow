import { useMemo, useRef, useState } from "react";
import type {
  ThroughputBucket,
  ThroughputInterval,
} from "../../lib/api";

interface ThroughputChartProps {
  buckets: ThroughputBucket[];
  interval: ThroughputInterval;
  /** Caller-controlled override; null means "auto-picked by server." */
  selectedInterval: ThroughputInterval | null;
  onIntervalChange: (i: ThroughputInterval | null) => void;
}

const HEIGHT = 200;
const PAD_TOP = 8;
const PAD_BOTTOM = 28;
const PAD_LEFT = 32;
const PAD_RIGHT = 8;

// Order matters for the stack — render running/failed/success
// bottom-to-top so the most-engaged-with categories (failed,
// success) sit at the top of each stacked column where they're
// easier to compare across buckets.
const STATE_STACK_ORDER: { key: string; label: string; cssVar: string }[] = [
  { key: "queued", label: "Queued", cssVar: "--color-muted" },
  { key: "running", label: "Running", cssVar: "--color-accent" },
  { key: "failed", label: "Failed", cssVar: "--color-error" },
  { key: "success", label: "Succeeded", cssVar: "--color-ink" },
];

/** Stacked area-ish chart: one stacked vertical bar per time bucket,
 *  segments colored by state. We use rectangles per (bucket, state)
 *  rather than a smoothed area path because the data is intrinsically
 *  bucketed (daily/weekly/monthly counts), not continuous.
 *
 *  Interval selector at the top lets the user override the auto-
 *  picked bucket size. Null = "use whatever the server auto-picked
 *  based on date range." */
export function ThroughputChart({
  buckets,
  interval,
  selectedInterval,
  onIntervalChange,
}: ThroughputChartProps) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [hover, setHover] = useState<
    { idx: number; x: number; y: number } | null
  >(null);

  const layout = useMemo(() => {
    if (buckets.length === 0) return null;
    const totals = buckets.map((b) =>
      Object.values(b.counts).reduce((s, n) => s + n, 0),
    );
    const maxTotal = Math.max(1, ...totals);
    const width = 600;
    const chartW = width - PAD_LEFT - PAD_RIGHT;
    const chartH = HEIGHT - PAD_TOP - PAD_BOTTOM;
    const colW = chartW / buckets.length;
    return {
      width,
      maxTotal,
      chartH,
      colW,
      cols: buckets.map((b, i) => {
        const total = totals[i];
        const x = PAD_LEFT + i * colW;
        const segments: { state: string; y: number; h: number; count: number }[] = [];
        let yCursor = PAD_TOP + chartH;
        for (const { key } of STATE_STACK_ORDER) {
          const count = b.counts[key] ?? 0;
          if (count === 0) continue;
          const h = (count / maxTotal) * chartH;
          yCursor -= h;
          segments.push({ state: key, y: yCursor, h, count });
        }
        return { x, segments, bucket: b, total, idx: i };
      }),
    };
  }, [buckets]);

  function trackXY(e: React.PointerEvent): { x: number; y: number } {
    const r = wrapRef.current?.getBoundingClientRect();
    return r ? { x: e.clientX - r.left, y: e.clientY - r.top } : { x: 0, y: 0 };
  }

  // Render the interval picker above the chart unconditionally;
  // even an empty-data chart benefits from showing the active
  // interval and letting the user switch.
  return (
    <div>
      <div className="mb-3 flex items-center gap-2">
        <span className="font-sans text-xs uppercase tracking-[0.12em] text-muted">
          Interval:
        </span>
        {(["day", "week", "month"] as ThroughputInterval[]).map((i) => {
          const isActive = selectedInterval
            ? selectedInterval === i
            : interval === i;
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
              {i}
            </button>
          );
        })}
        {selectedInterval ? (
          <span className="font-sans text-xs text-muted">(override)</span>
        ) : (
          <span className="font-sans text-xs text-muted">(auto)</span>
        )}
      </div>
      {layout ? (
        <div ref={wrapRef} className="relative">
          <svg
            viewBox={`0 0 ${layout.width} ${HEIGHT}`}
            className="w-full"
            style={{ maxHeight: HEIGHT }}
            onPointerLeave={() => setHover(null)}
          >
            {/* Y-axis ticks — 0 and max */}
            <text
              x={PAD_LEFT - 4}
              y={PAD_TOP + 8}
              textAnchor="end"
              fontSize={9}
              fill="rgb(var(--color-muted))"
              fontFamily="var(--font-mono)"
            >
              {layout.maxTotal}
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
            {/* Columns — one rect per state segment */}
            {layout.cols.map((c) =>
              c.segments.map((seg) => (
                <rect
                  key={`${c.idx}-${seg.state}`}
                  x={c.x}
                  y={seg.y}
                  width={Math.max(1, layout.colW - 1)}
                  height={seg.h}
                  fill={`rgb(var(${
                    STATE_STACK_ORDER.find((s) => s.key === seg.state)
                      ?.cssVar ?? "--color-muted"
                  }))`}
                  fillOpacity={hover?.idx === c.idx ? 1 : 0.85}
                  onPointerEnter={(e) =>
                    setHover({ idx: c.idx, ...trackXY(e) })
                  }
                  onPointerMove={(e) =>
                    setHover((h) =>
                      h && h.idx === c.idx ? { ...h, ...trackXY(e) } : h,
                    )
                  }
                />
              )),
            )}
            {/* X-axis ticks — first, middle, last */}
            {[0, Math.floor(layout.cols.length / 2), layout.cols.length - 1]
              .filter((v, i, a) => a.indexOf(v) === i)
              .map((i) => {
                const c = layout.cols[i];
                return (
                  <text
                    key={`xt-${i}`}
                    x={c.x + layout.colW / 2}
                    y={HEIGHT - 8}
                    textAnchor="middle"
                    fontSize={9}
                    fill="rgb(var(--color-muted))"
                    fontFamily="var(--font-mono)"
                  >
                    {formatBucketStart(c.bucket.start, interval)}
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
                {formatBucketStart(
                  layout.cols[hover.idx].bucket.start,
                  interval,
                )}
              </div>
              <div className="opacity-70 mt-0.5">
                Total: {layout.cols[hover.idx].total}
              </div>
              {STATE_STACK_ORDER.map((s) => {
                const c = layout.cols[hover.idx].bucket.counts[s.key] ?? 0;
                if (c === 0) return null;
                return (
                  <div key={s.key} className="opacity-70">
                    {s.label}: {c}
                  </div>
                );
              })}
            </div>
          ) : null}
        </div>
      ) : (
        <p className="text-xs text-muted">No submissions in this range.</p>
      )}
      {/* Legend matches the stack order for consistency. */}
      <div className="mt-3 flex flex-wrap items-center gap-4 text-xs text-muted">
        {STATE_STACK_ORDER.map((s) => (
          <span key={s.key} className="inline-flex items-center gap-2">
            <span
              aria-hidden="true"
              className="inline-block h-3 w-3 border border-border"
              style={{
                backgroundColor: `rgb(var(${s.cssVar}))`,
                opacity: 0.85,
              }}
            />
            {s.label}
          </span>
        ))}
      </div>
    </div>
  );
}

function formatBucketStart(iso: string, interval: ThroughputInterval): string {
  const d = new Date(iso);
  if (interval === "month") {
    return d.toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
    });
  }
  return d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}
