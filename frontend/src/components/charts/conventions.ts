import { useEffect, useMemo, useRef, useState } from "react";
import { scaleBand, scaleLinear } from "@visx/scale";
import { max as d3max } from "d3-array";

/**
 * Conventions shared across all chart components in this directory.
 *
 * COLORS — always use CSS variables from the theme:
 *   "rgb(var(--color-accent))"    primary data series, brand-colored
 *   "rgb(var(--color-ink))"       secondary series, dark
 *   "rgb(var(--color-muted))"     axis lines, tick labels
 *   "rgb(var(--color-border))"    grid lines if used
 *   "rgb(var(--color-surface))"   background fills
 *
 * TYPOGRAPHY — axis labels use mono and small sizes:
 *   fontFamily: "var(--font-mono)"
 *   fontSize: 10
 *
 * LAYOUT — every chart uses margin-based layout. The SVG is the full
 * width × height; the chart drawing area is inset by margins for axes.
 */

export const DEFAULT_MARGIN = {
  top: 12,
  right: 12,
  bottom: 30,
  left: 44,
};

export interface ChartMargin {
  top: number;
  right: number;
  bottom: number;
  left: number;
}

/** Standard axis tick label props for @visx/axis. */
export function axisLabelProps(
  extra: { dx?: number; textAnchor?: "start" | "middle" | "end" } = {},
) {
  return () => ({
    fill: "rgb(var(--color-muted))",
    fontFamily: "var(--font-mono)",
    fontSize: 10,
    textAnchor: extra.textAnchor ?? "middle",
    ...(extra.dx !== undefined ? { dx: extra.dx } : {}),
  });
}

/**
 * Measures the width of a container element via ResizeObserver. Returns
 * [ref, width]. Attach the ref to a block-level container; the chart's
 * SVG can then use this width for its viewBox.
 */
export function useChartWidth(initial = 480): [
  React.RefObject<HTMLDivElement>,
  number,
] {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(initial);

  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0].contentRect.width;
      if (w > 0) setWidth(w);
    });
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);

  return [ref, width];
}

// --- Histogram helpers ------------------------------------------------------

export interface HistogramDatum {
  label: string;
  count: number;
}

export interface HistogramScales {
  xScale: ReturnType<typeof scaleBand<string>>;
  yScale: ReturnType<typeof scaleLinear<number>>;
  bandwidth: number;
  step: number;
  /** Map a bar index to its x pixel position. */
  xOf: (index: number) => number;
  /** Map a pixel x to the nearest bar index (clamped to [0, len-1]). */
  xToIndex: (x: number) => number;
}

/**
 * Build the scales for a histogram. Shared by the standalone
 * `<Histogram>` component and any widget that needs to render bars
 * with custom interaction overlays. Memoized on data + width + height.
 *
 * Using this hook (rather than scaleBand/scaleLinear directly) keeps
 * the bar gap, padding, and step calculations consistent everywhere a
 * histogram appears in the application.
 */
export function useHistogramScales(
  data: HistogramDatum[],
  width: number,
  chartHeight: number,
  vertPad: number = 8,
): HistogramScales {
  return useMemo(() => {
    const padding = data.length > 30 ? 0.1 : 0.15;
    const xScale = scaleBand<string>({
      domain: data.map((d) => d.label),
      range: [0, width],
      paddingInner: padding,
    });
    const yScale = scaleLinear<number>({
      domain: [0, d3max(data, (d) => d.count) ?? 1],
      range: [chartHeight - vertPad, vertPad],
    });
    const bandwidth = xScale.bandwidth();
    const step = data.length > 0 ? xScale.step() : 0;
    const xOf = (i: number) =>
      data[i] !== undefined ? xScale(data[i].label) ?? 0 : 0;
    const xToIndex = (x: number) => {
      if (data.length === 0 || step === 0) return 0;
      const i = Math.round(x / step);
      return i < 0 ? 0 : i >= data.length ? data.length - 1 : i;
    };
    return { xScale, yScale, bandwidth, step, xOf, xToIndex };
  }, [data, width, chartHeight, vertPad]);
}
