import { useMemo } from "react";
import { scaleLinear, scaleTime } from "@visx/scale";
import { AxisBottom, AxisLeft } from "@visx/axis";
import { LinePath } from "@visx/shape";
import { extent, max } from "d3-array";
import { timeParse } from "d3-time-format";
import {
  DEFAULT_MARGIN,
  axisLabelProps,
  useChartWidth,
  type ChartMargin,
} from "./conventions";

/**
 * Line chart over time. Each data point is an ISO date string + a numeric
 * value. Time scale infers domain from the data extent; linear scale on y.
 *
 * Use this for "value over time" reporting: KPI trends, run frequencies,
 * record counts over weeks, etc.
 */
export interface TimeSeriesPoint {
  /** ISO date string, YYYY-MM-DD. */
  date: string;
  value: number;
}

export interface TimeSeriesChartProps {
  data: TimeSeriesPoint[];
  height?: number;
  yLabel?: string;
  /** Override the default margins if axes need more room. */
  margin?: ChartMargin;
}

const parseISO = timeParse("%Y-%m-%d");

export function TimeSeriesChart({
  data,
  height = 240,
  yLabel,
  margin = DEFAULT_MARGIN,
}: TimeSeriesChartProps) {
  const [containerRef, width] = useChartWidth();

  const points = useMemo(
    () =>
      data
        .map((d) => ({ date: parseISO(d.date), value: d.value }))
        .filter((p): p is { date: Date; value: number } => p.date !== null),
    [data],
  );

  const innerW = Math.max(0, width - margin.left - margin.right);
  const innerH = Math.max(0, height - margin.top - margin.bottom);

  const xScale = useMemo(() => {
    const [d0, d1] = extent(points, (p) => p.date) as [Date, Date];
    return scaleTime<number>({
      domain: [d0 ?? new Date(0), d1 ?? new Date()],
      range: [0, innerW],
    });
  }, [points, innerW]);

  const yScale = useMemo(
    () =>
      scaleLinear<number>({
        domain: [0, max(points, (p) => p.value) ?? 1],
        range: [innerH, 0],
        nice: true,
      }),
    [points, innerH],
  );

  if (points.length === 0) {
    return (
      <div ref={containerRef} className="w-full">
        <p className="text-sm text-muted italic">No data to chart.</p>
      </div>
    );
  }

  return (
    <div ref={containerRef} className="w-full">
      <svg
        width="100%"
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        style={{ display: "block" }}
      >
        <g transform={`translate(${margin.left}, ${margin.top})`}>
          <LinePath
            data={points}
            x={(p) => xScale(p.date) ?? 0}
            y={(p) => yScale(p.value) ?? 0}
            stroke="rgb(var(--color-accent))"
            strokeWidth={1.5}
            strokeLinejoin="round"
          />
          <AxisBottom
            top={innerH}
            scale={xScale}
            numTicks={Math.min(5, points.length)}
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
