import { useMemo } from "react";
import { scaleBand, scaleLinear } from "@visx/scale";
import { AxisBottom, AxisLeft } from "@visx/axis";
import { Bar } from "@visx/shape";
import { max } from "d3-array";
import {
  DEFAULT_MARGIN,
  axisLabelProps,
  useChartWidth,
  type ChartMargin,
} from "./conventions";

/**
 * Vertical bar chart over categorical data. Each datum is a string label
 * plus a numeric value.
 *
 * Use this for category comparisons: counts per type, totals per region,
 * etc. For time-based bars (running totals over weeks), TimeSeriesChart
 * is usually the better fit.
 */
export interface BarDatum {
  label: string;
  value: number;
}

export interface BarChartProps {
  data: BarDatum[];
  height?: number;
  yLabel?: string;
  margin?: ChartMargin;
}

export function BarChart({
  data,
  height = 240,
  yLabel,
  margin = DEFAULT_MARGIN,
}: BarChartProps) {
  const [containerRef, width] = useChartWidth();

  const innerW = Math.max(0, width - margin.left - margin.right);
  const innerH = Math.max(0, height - margin.top - margin.bottom);

  const xScale = useMemo(
    () =>
      scaleBand<string>({
        domain: data.map((d) => d.label),
        range: [0, innerW],
        paddingInner: data.length > 30 ? 0.1 : 0.2,
      }),
    [data, innerW],
  );

  const yScale = useMemo(
    () =>
      scaleLinear<number>({
        domain: [0, max(data, (d) => d.value) ?? 1],
        range: [innerH, 0],
        nice: true,
      }),
    [data, innerH],
  );

  if (data.length === 0) {
    return (
      <div ref={containerRef} className="w-full">
        <p className="text-sm text-muted italic">No data to chart.</p>
      </div>
    );
  }

  // Pick at most 8 evenly-spaced labels for the x-axis to avoid overlap.
  const labelStep = Math.max(1, Math.ceil(data.length / 8));
  const tickValues = data
    .filter((_, i) => i % labelStep === 0)
    .map((d) => d.label);

  return (
    <div ref={containerRef} className="w-full">
      <svg
        width="100%"
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        style={{ display: "block" }}
      >
        <g transform={`translate(${margin.left}, ${margin.top})`}>
          {data.map((d) => (
            <Bar
              key={d.label}
              x={xScale(d.label) ?? 0}
              y={yScale(d.value) ?? 0}
              width={xScale.bandwidth()}
              height={innerH - (yScale(d.value) ?? 0)}
              fill="rgb(var(--color-accent))"
            />
          ))}
          <AxisBottom
            top={innerH}
            scale={xScale}
            tickValues={tickValues}
            tickStroke="rgb(var(--color-muted))"
            stroke="rgb(var(--color-muted))"
            tickLabelProps={axisLabelProps()}
          />
          <AxisLeft
            scale={yScale}
            numTicks={4}
            tickStroke="rgb(var(--color-muted))"
            stroke="rgb(var(--color-muted))"
            tickLabelProps={axisLabelProps({ dx: -4, textAnchor: "end" })}
            label={yLabel}
            labelOffset={32}
            labelProps={{
              fill: "rgb(var(--color-muted))",
              fontFamily: "var(--font-mono)",
              fontSize: 10,
              textAnchor: "middle",
            }}
          />
        </g>
      </svg>
    </div>
  );
}
