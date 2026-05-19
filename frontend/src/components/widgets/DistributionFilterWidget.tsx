import {
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { timeFormat, timeParse } from "d3-time-format";
import {
  Histogram,
  type HistogramDatum,
  type HistogramScales,
} from "../charts";
import { BaseWidget } from "./BaseWidget";
import { type Widget, type WidgetProps } from "./types";

/**
 * Distribution filter widget. Renders a histogram with a translucent
 * selection overlay and two draggable handles. The body of the
 * selection can also be dragged to translate the whole range.
 *
 * Built on the standalone <Histogram> component — the bars and axis
 * come from there, while the selection overlay, handles, drag
 * interaction, and "out of range" tooltip are added by this widget.
 *
 * Value shape: { start: string, end: string } — labels at the bounds.
 */
export interface DistributionFilterValue {
  start: string;
  end: string;
}

type DragKind = "start" | "end" | "body";

const CHART_H = 170;
const AXIS_H = 22;
const TOTAL_H = CHART_H + AXIS_H;
const HANDLE_W = 3;
const HANDLE_HIT = 14;
const HANDLE_OVERHANG = 4;

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const parseISO = timeParse("%Y-%m-%d");
const formatShort = timeFormat("%b %-d");

function DistributionFilterComponent({
  field,
  xcom,
  value,
  onChange,
  error,
}: WidgetProps<DistributionFilterValue>) {
  const xcomKey = (field.widget_data?.xcom_key as string | undefined) ?? "";
  const valueLabel =
    (field.widget_data?.value_label as string | undefined) ?? "value";
  const rawData = xcomKey
    ? (xcom[xcomKey] as Record<string, number> | undefined)
    : undefined;

  const data: HistogramDatum[] = rawData
    ? Object.entries(rawData).map(([label, count]) => ({ label, count }))
    : [];

  // Seed once with the full range so non-interacting users still submit
  // a meaningful selection.
  const seededRef = useRef(false);
  useEffect(() => {
    if (seededRef.current || data.length === 0) return;
    if (!value) {
      onChange({ start: data[0].label, end: data[data.length - 1].label });
    }
    seededRef.current = true;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data.length]);

  const svgRef = useRef<SVGSVGElement>(null);
  // Stash the chart's scales from Histogram's render-prop callback so the
  // pointer event handlers (registered on the SVG via svgProps) can
  // translate pixel x → bar index using the same scale instance.
  const scalesRef = useRef<HistogramScales | null>(null);

  const [dragging, setDragging] = useState<DragKind | null>(null);
  const dragAnchorRef = useRef({ x: 0, startIdx: 0, endIdx: 0 });
  const [hoveredIdx, setHoveredIdx] = useState<number | null>(null);

  if (data.length === 0) {
    return (
      <BaseWidget label={field.label} error={error}>
        <p className="text-sm text-muted italic">
          No distribution data available.
        </p>
      </BaseWidget>
    );
  }

  const selection = value ?? {
    start: data[0].label,
    end: data[data.length - 1].label,
  };
  const findIdx = (label: string) =>
    clamp(
      data.findIndex((d) => d.label === label),
      0,
      data.length - 1,
    );
  const startIdx = findIdx(selection.start);
  const endIdx = findIdx(selection.end);
  const lo = Math.min(startIdx, endIdx);
  const hi = Math.max(startIdx, endIdx);
  const selectedCount = data
    .slice(lo, hi + 1)
    .reduce((sum, d) => sum + d.count, 0);

  const onPtrDown = (e: ReactPointerEvent<SVGElement>, what: DragKind) => {
    if (!svgRef.current) return;
    const rect = svgRef.current.getBoundingClientRect();
    const x = e.clientX - rect.left;
    dragAnchorRef.current = { x, startIdx: lo, endIdx: hi };
    setDragging(what);
    try {
      (e.target as Element).setPointerCapture(e.pointerId);
    } catch {
      /* not critical */
    }
  };

  const onPtrMove = (e: ReactPointerEvent<SVGSVGElement>) => {
    const scales = scalesRef.current;
    if (!scales || !svgRef.current) return;
    const rect = svgRef.current.getBoundingClientRect();
    const x = e.clientX - rect.left;
    setHoveredIdx(scales.xToIndex(x));

    if (!dragging) return;
    const anchor = dragAnchorRef.current;
    if (dragging === "start") {
      const idx = clamp(scales.xToIndex(x), 0, anchor.endIdx);
      onChange({ start: data[idx].label, end: data[anchor.endIdx].label });
    } else if (dragging === "end") {
      const idx = clamp(scales.xToIndex(x), anchor.startIdx, data.length - 1);
      onChange({ start: data[anchor.startIdx].label, end: data[idx].label });
    } else {
      const deltaIdx =
        scales.xToIndex(x) - scales.xToIndex(anchor.x);
      const span = anchor.endIdx - anchor.startIdx;
      const newLo = clamp(
        anchor.startIdx + deltaIdx,
        0,
        data.length - 1 - span,
      );
      onChange({ start: data[newLo].label, end: data[newLo + span].label });
    }
  };

  const onPtrUp = (e: ReactPointerEvent<SVGSVGElement>) => {
    if (!dragging) return;
    try {
      (e.target as Element).releasePointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
    setDragging(null);
  };

  return (
    <BaseWidget label={field.label} error={error}>
      <div className="flex justify-between items-end gap-4 mb-3">
        <div className="flex flex-col gap-0.5">
          <span className="text-[10px] uppercase tracking-[0.2em] text-muted font-mono">
            From
          </span>
          <span className="font-mono text-sm text-ink">{selection.start}</span>
        </div>
        <button
          type="button"
          onClick={() =>
            onChange({
              start: data[0].label,
              end: data[data.length - 1].label,
            })
          }
          className="text-[10px] uppercase tracking-[0.2em] text-muted hover:text-ink underline underline-offset-2 mb-1"
        >
          Reset
        </button>
        <div className="flex flex-col gap-0.5 text-right">
          <span className="text-[10px] uppercase tracking-[0.2em] text-muted font-mono">
            To
          </span>
          <span className="font-mono text-sm text-ink">{selection.end}</span>
        </div>
      </div>

      <div className="bg-surface border border-border px-1 pt-2 pb-1">
        <Histogram
          data={data}
          height={TOTAL_H}
          barOpacity={(_, i) => (i >= lo && i <= hi ? 0.85 : 0.2)}
          formatTick={prettyLabel}
          numTicks={5}
          disableTooltip
          svgRef={svgRef}
          svgProps={{
            onPointerMove: onPtrMove,
            onPointerUp: onPtrUp,
            onPointerLeave: () => setHoveredIdx(null),
          }}
          renderOverlay={(scales, { chartH, vertPad }) => {
            scalesRef.current = scales;
            return (
              <>
                {/* Translucent selection rectangle (also the body-drag target) */}
                <rect
                  x={scales.xOf(lo)}
                  y={vertPad / 2}
                  width={scales.xOf(hi) + scales.bandwidth - scales.xOf(lo)}
                  height={chartH - vertPad}
                  fill="rgb(var(--color-accent))"
                  fillOpacity={0.1}
                  onPointerDown={(e) => onPtrDown(e, "body")}
                  style={{ cursor: dragging === "body" ? "grabbing" : "grab" }}
                />
                <Handle
                  x={scales.xOf(lo) - HANDLE_W / 2}
                  yTop={vertPad / 2}
                  height={chartH - vertPad}
                  onPointerDown={(e) => onPtrDown(e, "start")}
                  active={dragging === "start"}
                />
                <Handle
                  x={scales.xOf(hi) + scales.bandwidth - HANDLE_W / 2}
                  yTop={vertPad / 2}
                  height={chartH - vertPad}
                  onPointerDown={(e) => onPtrDown(e, "end")}
                  active={dragging === "end"}
                />
              </>
            );
          }}
          renderHtmlOverlay={({ scales }) => {
            if (hoveredIdx === null || dragging) return null;
            const d = data[hoveredIdx];
            if (!d) return null;
            const inSelection = hoveredIdx >= lo && hoveredIdx <= hi;
            // 4 / 8 here = container padding (px-1 pt-2 = 4px / 8px)
            return (
              <div
                className="absolute pointer-events-none bg-ink text-bg font-mono text-[11px] px-2 py-1 whitespace-nowrap shadow-sm"
                style={{
                  left: `${4 + scales.xOf(hoveredIdx) + scales.bandwidth / 2}px`,
                  top: `${8 + (scales.yScale(d.count) ?? 0)}px`,
                  transform: "translate(-50%, calc(-100% - 6px))",
                }}
              >
                <div>{d.label}</div>
                <div className="opacity-70">
                  {d.count.toLocaleString()} {valueLabel}
                  {inSelection ? "" : " · out of range"}
                </div>
              </div>
            );
          }}
        />
      </div>

      <p className="mt-2 text-xs text-muted font-mono">
        {selectedCount.toLocaleString()} {valueLabel} in range · {hi - lo + 1}{" "}
        of {data.length} buckets
      </p>
    </BaseWidget>
  );
}

function Handle({
  x,
  yTop,
  height,
  onPointerDown,
  active,
}: {
  x: number;
  yTop: number;
  height: number;
  onPointerDown: (e: ReactPointerEvent<SVGElement>) => void;
  active: boolean;
}) {
  return (
    <g style={{ cursor: "col-resize" }}>
      <rect
        x={x - (HANDLE_HIT - HANDLE_W) / 2}
        y={yTop - HANDLE_OVERHANG}
        width={HANDLE_HIT}
        height={height + 2 * HANDLE_OVERHANG}
        fill="transparent"
        onPointerDown={onPointerDown}
      />
      <rect
        x={x}
        y={yTop - HANDLE_OVERHANG}
        width={HANDLE_W}
        height={height + 2 * HANDLE_OVERHANG}
        fill="rgb(var(--color-accent))"
        opacity={active ? 1 : 0.9}
        style={{ pointerEvents: "none" }}
      />
      {[-6, 0, 6].map((dy) => (
        <circle
          key={dy}
          cx={x + HANDLE_W / 2}
          cy={yTop + height / 2 + dy}
          r={1.1}
          fill="rgb(var(--color-bg))"
          style={{ pointerEvents: "none" }}
        />
      ))}
    </g>
  );
}

function clamp(n: number, lo: number, hi: number): number {
  if (Number.isNaN(n)) return lo;
  return n < lo ? lo : n > hi ? hi : n;
}

/**
 * Format an x-axis key for display. The histogram is a generic range
 * filter — keys may be ISO dates or numbers. Dates render as "Jan 6";
 * numbers render with locale grouping; anything else is shown as-is.
 */
function prettyLabel(label: string): string {
  if (ISO_DATE.test(label)) {
    const date = parseISO(label);
    if (date) return formatShort(date);
  }
  if (label !== "" && !Number.isNaN(Number(label))) {
    return Number(label).toLocaleString();
  }
  return label;
}

// --- Widget bundle ----------------------------------------------------------

export const distributionFilterWidget: Widget<DistributionFilterValue> = {
  Component: DistributionFilterComponent,
  renderSubmitted: (value) => {
    if (!value) return "—";
    return `${value.start} → ${value.end}`;
  },
  validate: (value, field) => {
    if (!field.required) return null;
    if (!value || !value.start || !value.end) {
      return `${field.label} is required`;
    }
    return null;
  },
};
