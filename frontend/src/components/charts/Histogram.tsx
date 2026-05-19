import { useState, type ReactNode } from "react";
import { AxisBottom } from "@visx/axis";
import { Bar } from "@visx/shape";
import {
  axisLabelProps,
  useChartWidth,
  useHistogramScales,
  type HistogramDatum,
  type HistogramScales,
} from "./conventions";

/**
 * Histogram chart. A column for each bucket; tooltip on hover. No
 * selection or range concept — that lives in `DistributionFilterWidget`,
 * which uses the same scales (via `useHistogramScales`) and adds a
 * selection overlay on top.
 *
 * Use this in non-widget contexts — for example, a report node showing
 * the distribution of records over time without asking for input.
 *
 * Consumers that need to overlay custom SVG content (selection
 * rectangles, annotations, etc.) can pass a `renderOverlay` render prop
 * to layer additional SVG elements onto the chart's coordinate system.
 * Custom HTML overlays (e.g., interactive tooltips) go via
 * `renderHtmlOverlay`.
 */
export interface HistogramProps {
  data: HistogramDatum[];
  /** Total height including the x-axis. */
  height?: number;
  /** Per-bar opacity. Defaults to 0.85. */
  barOpacity?: (datum: HistogramDatum, index: number) => number;
  /** Per-bar fill. Defaults to ink. */
  barFill?: (datum: HistogramDatum, index: number) => string;
  /** Tick label formatter. */
  formatTick?: (label: string) => string;
  /** Up to N ticks evenly spaced across the data. */
  numTicks?: number;
  /** Disable the built-in tooltip (e.g., a widget provides its own). */
  disableTooltip?: boolean;
  /**
   * SVG content rendered on top of bars but below the axis. Receives
   * the chart's scales and layout helpers.
   */
  renderOverlay?: (scales: HistogramScales, layout: { chartH: number; vertPad: number }) => ReactNode;
  /**
   * HTML content rendered as absolutely-positioned children of the
   * chart container. Use for HTML tooltips, custom hover cards, etc.
   * Receives the same scales/layout, plus the current hovered index
   * (managed internally unless disableTooltip is true).
   */
  renderHtmlOverlay?: (params: {
    scales: HistogramScales;
    chartH: number;
    vertPad: number;
    hoveredIdx: number | null;
  }) => ReactNode;
  /** Override the SVG element's props (refs, event handlers, etc.). */
  svgProps?: Omit<
    React.SVGProps<SVGSVGElement>,
    "width" | "height" | "viewBox" | "ref"
  >;
  /** Forwarded ref to the SVG. */
  svgRef?: React.RefObject<SVGSVGElement>;
}

const AXIS_H = 22;
const VERT_PAD = 8;

export function Histogram({
  data,
  height = 200,
  barOpacity,
  barFill,
  formatTick,
  numTicks = 5,
  disableTooltip = false,
  renderOverlay,
  renderHtmlOverlay,
  svgProps,
  svgRef,
}: HistogramProps) {
  const [containerRef, width] = useChartWidth();
  const chartH = height - AXIS_H;
  const scales = useHistogramScales(data, width, chartH, VERT_PAD);
  const [hoveredIdx, setHoveredIdx] = useState<number | null>(null);

  if (data.length === 0) {
    return (
      <div ref={containerRef} className="w-full">
        <p className="text-sm text-muted italic">No data to chart.</p>
      </div>
    );
  }

  const tickValues =
    data.length > 1 && numTicks > 1
      ? Array.from({ length: Math.min(numTicks, data.length) }, (_, i) =>
          data[
            Math.round(
              (i * (data.length - 1)) /
                (Math.min(numTicks, data.length) - 1),
            )
          ].label,
        )
      : [data[0].label];

  return (
    <div
      ref={containerRef}
      className="relative w-full"
      style={{ touchAction: svgProps?.onPointerDown ? "none" : undefined }}
    >
      <svg
        ref={svgRef}
        width="100%"
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        style={{ display: "block", overflow: "visible" }}
        {...svgProps}
      >
        {data.map((d, i) => (
          <Bar
            key={d.label}
            x={scales.xOf(i)}
            y={scales.yScale(d.count) ?? 0}
            width={scales.bandwidth}
            height={chartH - VERT_PAD - (scales.yScale(d.count) ?? 0)}
            fill={barFill ? barFill(d, i) : "rgb(var(--color-ink))"}
            opacity={barOpacity ? barOpacity(d, i) : 0.85}
          />
        ))}

        {renderOverlay ? renderOverlay(scales, { chartH, vertPad: VERT_PAD }) : null}

        <AxisBottom
          top={chartH}
          scale={scales.xScale}
          tickValues={tickValues}
          tickFormat={(label) => (formatTick ? formatTick(String(label)) : String(label))}
          tickStroke="rgb(var(--color-muted))"
          stroke="rgb(var(--color-muted))"
          hideAxisLine
          tickLength={4}
          tickLabelProps={axisLabelProps()}
        />

        {/* Internal hover-tracking layer — invisible, fills the chart */}
        {!disableTooltip ? (
          <rect
            x={0}
            y={0}
            width={width}
            height={chartH}
            fill="transparent"
            onPointerMove={(e) => {
              const rect = (e.currentTarget.ownerSVGElement as SVGSVGElement).getBoundingClientRect();
              setHoveredIdx(scales.xToIndex(e.clientX - rect.left));
            }}
            onPointerLeave={() => setHoveredIdx(null)}
          />
        ) : null}
      </svg>

      {/* Built-in tooltip — suppressed if consumer provides its own */}
      {!disableTooltip && hoveredIdx !== null ? (
        <BuiltInTooltip
          x={4 + scales.xOf(hoveredIdx) + scales.bandwidth / 2}
          y={8 + (scales.yScale(data[hoveredIdx].count) ?? 0)}
          label={data[hoveredIdx].label}
          count={data[hoveredIdx].count}
        />
      ) : null}

      {renderHtmlOverlay
        ? renderHtmlOverlay({ scales, chartH, vertPad: VERT_PAD, hoveredIdx })
        : null}
    </div>
  );
}

function BuiltInTooltip({
  x,
  y,
  label,
  count,
}: {
  x: number;
  y: number;
  label: string;
  count: number;
}) {
  return (
    <div
      className="absolute pointer-events-none bg-ink text-bg font-mono text-[11px] px-2 py-1 whitespace-nowrap shadow-sm"
      style={{
        left: `${x}px`,
        top: `${y}px`,
        transform: "translate(-50%, calc(-100% - 6px))",
      }}
    >
      <div>{label}</div>
      <div className="opacity-70">{count.toLocaleString()}</div>
    </div>
  );
}
