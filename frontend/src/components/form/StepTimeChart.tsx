import { useRef, useState } from "react";
import type { StepTimeBucket } from "../../lib/api";

interface StepTimeChartProps {
  steps: StepTimeBucket[];
  /** Currently expanded step (drilldown). Null = nothing expanded. */
  expandedNodeId?: string | null;
  onBarClick?: (nodeId: string) => void;
  /** Scale mode — controlled by the parent. The parent owns this
   *  (typically URL-backed) so the page state is shareable. */
  scaleMode: ScaleMode;
  onScaleModeChange: (m: ScaleMode) => void;
}

const ROW_HEIGHT = 28;
const LABEL_WIDTH = 140;
const COUNT_WIDTH = 40;
const TIME_WIDTH = 60;

type ScaleMode = "shared" | "per-step";

/** Horizontal bars with mean ± p10/p90 whiskers per step. Each row is:
 *
 *     ▸ STEP NAME    |————●————|              23s   6
 *
 *  Where the bar area shows:
 *   - Vertical tick (●) at the mean
 *   - Light grey band from p10 to p90
 *   - End caps at the band edges
 *
 *  Trailing columns: mean time (formatted), then visit count.
 *  Column headers above the chart label the axis and the trailing
 *  numbers so the encoding is self-explanatory.
 *
 *  Two scale modes (togglable at the top):
 *   - **shared**: all rows share a common x-axis from 0 to the
 *     global max p90. Cross-step comparison is direct — a wider
 *     bar means more time. Suffers when one step's p90 dwarfs the
 *     others (small steps collapse into the left edge).
 *   - **per-step**: each row scales to its own p90. Whiskers fill
 *     the row width; the right-edge label shows what that row's
 *     max represents. Cross-row width comparison breaks (a wider
 *     bar doesn't mean more time) but every distribution is
 *     individually readable.
 *
 *  Click → caller decides what to do (typically toggle the inline
 *  drilldown histogram below the chart). */
export function StepTimeChart({
  steps,
  expandedNodeId,
  onBarClick,
  scaleMode,
  onScaleModeChange,
}: StepTimeChartProps) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [hover, setHover] = useState<
    | { idx: number; x: number; y: number }
    | null
  >(null);

  if (steps.length === 0) {
    return <p className="text-xs text-muted">No timing data.</p>;
  }

  // Global max p90 across all rows. Used as the shared-scale
  // denominator; also used in per-step mode as a fallback when a
  // row's p90 is zero.
  const globalMaxP90 = Math.max(
    1,
    ...steps.map((s) => s.p90_seconds),
  );

  function trackXY(e: React.PointerEvent): { x: number; y: number } {
    const r = wrapRef.current?.getBoundingClientRect();
    return r ? { x: e.clientX - r.left, y: e.clientY - r.top } : { x: 0, y: 0 };
  }

  return (
    <div ref={wrapRef} className="relative">
      {/* Scale-mode toggle. Shared = single axis across all rows
          (good for cross-step comparison). Per-step = each row
          normalizes to its own p90 (good when one step dwarfs
          the others). State is controlled by the parent and lives
          in the URL so the page is shareable. */}
      <div className="mb-3 flex items-center gap-2">
        <span className="font-sans text-xs uppercase tracking-[0.12em] text-muted">
          Scale:
        </span>
        {(["shared", "per-step"] as ScaleMode[]).map((m) => {
          const isActive = scaleMode === m;
          return (
            <button
              key={m}
              type="button"
              onClick={() => onScaleModeChange(m)}
              className={`font-mono text-xs px-2 py-0.5 border ${
                isActive
                  ? "border-ink bg-ink text-bg"
                  : "border-border text-muted hover:text-ink"
              }`}
            >
              {m === "shared" ? "shared" : "per step"}
            </button>
          );
        })}
      </div>
      {/* Column headers — make the encoding self-describing. The
          axis label depends on scale mode: shared shows a global
          max; per-step shows "row max" since each row has its own. */}
      <div
        className="mb-2 flex items-center gap-3 font-sans text-[10px] uppercase tracking-[0.14em] text-muted"
        style={{ paddingBottom: 4 }}
      >
        <span style={{ minWidth: LABEL_WIDTH, maxWidth: LABEL_WIDTH }}>
          Step
        </span>
        <span className="flex-1 flex items-center justify-between">
          <span>0</span>
          <span className="opacity-70">time</span>
          <span>
            {scaleMode === "shared"
              ? formatDuration(globalMaxP90)
              : "row max"}
          </span>
        </span>
        <span
          className="text-right"
          style={{ minWidth: TIME_WIDTH }}
        >
          mean
        </span>
        <span
          className="text-right"
          style={{ minWidth: COUNT_WIDTH }}
        >
          n
        </span>
      </div>
      <div className="flex flex-col gap-1">
        {steps.map((s, i) => {
          const isExpanded = expandedNodeId === s.node_id;
          // In shared mode every row uses globalMaxP90; in per-step
          // mode every row uses its own p90 (with global as fallback
          // when a row has zero p90 — degenerate single-sample case).
          const rowMax =
            scaleMode === "shared"
              ? globalMaxP90
              : Math.max(s.p90_seconds, 1);
          const trackPct = (v: number) => `${(v / rowMax) * 100}%`;
          return (
            <button
              key={s.node_id}
              type="button"
              onClick={onBarClick ? () => onBarClick(s.node_id) : undefined}
              disabled={!onBarClick}
              onPointerEnter={(e) => setHover({ idx: i, ...trackXY(e) })}
              onPointerMove={(e) =>
                setHover((h) =>
                  h && h.idx === i ? { ...h, ...trackXY(e) } : h,
                )
              }
              onPointerLeave={() => setHover(null)}
              className={`flex w-full items-center gap-3 text-left ${
                onBarClick ? "cursor-pointer" : "cursor-default"
              }`}
              style={{ height: ROW_HEIGHT }}
            >
              <span
                className="font-sans text-xs uppercase tracking-[0.12em] text-ink truncate"
                style={{ minWidth: LABEL_WIDTH, maxWidth: LABEL_WIDTH }}
              >
                {isExpanded ? "▾ " : "▸ "}
                {s.label}
              </span>
              {/* Bar/whisker track. The background tick at 0 anchors
                  the eye when all values are clustered near zero.
                  In per-step mode, an inline "max" label sits at the
                  far right of the track so the user knows what
                  "100% width" represents for this row. */}
              <div
                className="relative flex-1 border-l border-border"
                style={{ height: 14 }}
              >
                {/* p10–p90 whisker — a darker band so it reads
                    against the surface bg rather than blending in. */}
                <div
                  className="absolute top-1/2 -translate-y-1/2"
                  style={{
                    left: trackPct(s.p10_seconds),
                    width: trackPct(s.p90_seconds - s.p10_seconds),
                    height: 6,
                    backgroundColor: "rgb(var(--color-muted))",
                    opacity: 0.4,
                  }}
                />
                {/* Whisker endpoint caps — full-row vertical marks
                    so the band's extent is unambiguous. */}
                <div
                  className="absolute top-1/2 -translate-y-1/2"
                  style={{
                    left: trackPct(s.p10_seconds),
                    width: 1,
                    height: 10,
                    backgroundColor: "rgb(var(--color-muted))",
                    opacity: 0.7,
                  }}
                />
                <div
                  className="absolute top-1/2 -translate-y-1/2"
                  style={{
                    left: trackPct(s.p90_seconds),
                    width: 1,
                    height: 10,
                    backgroundColor: "rgb(var(--color-muted))",
                    opacity: 0.7,
                  }}
                />
                {/* Mean tick — thicker and full row height so it's
                    visible even when pressed against the left edge. */}
                <div
                  className="absolute top-0"
                  style={{
                    left: trackPct(s.mean_seconds),
                    width: 3,
                    height: "100%",
                    marginLeft: -1.5,
                    backgroundColor: "rgb(var(--color-ink))",
                  }}
                />
                {/* Per-row max label — only in per-step mode. Sits
                    at the right edge of the track since in per-step
                    mode the row's p90 fills the full track width
                    by definition. In shared mode the global max is
                    in the column header and this is hidden. */}
                {scaleMode === "per-step" ? (
                  <span
                    className="absolute font-mono text-[9px] text-muted bg-surface px-1"
                    style={{
                      right: 0,
                      top: "50%",
                      transform: "translateY(-50%)",
                    }}
                  >
                    {formatDuration(s.p90_seconds)}
                  </span>
                ) : null}
              </div>
              <span
                className="font-mono text-xs text-ink text-right"
                style={{ minWidth: TIME_WIDTH }}
              >
                {formatDuration(s.mean_seconds)}
              </span>
              <span
                className="font-mono text-xs text-muted text-right"
                style={{ minWidth: COUNT_WIDTH }}
              >
                {s.count}
              </span>
            </button>
          );
        })}
      </div>
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
            {steps[hover.idx].label}
          </div>
          <div className="opacity-70 mt-0.5">
            n = {steps[hover.idx].count}
          </div>
          <div className="opacity-70">
            mean {formatDuration(steps[hover.idx].mean_seconds)}
          </div>
          <div className="opacity-70">
            p10 {formatDuration(steps[hover.idx].p10_seconds)} · p50{" "}
            {formatDuration(steps[hover.idx].p50_seconds)} · p90{" "}
            {formatDuration(steps[hover.idx].p90_seconds)}
          </div>
        </div>
      ) : null}
    </div>
  );
}

/** Mirror of the server's `_format_duration` so the chart axes and
 *  tooltips read consistently. */
export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(0)}s`;
  if (seconds < 3600) return `${(seconds / 60).toFixed(0)}m`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}h`;
  return `${(seconds / 86400).toFixed(1)}d`;
}
