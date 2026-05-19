import { useMemo } from "react";
import { mean, median, min, max, sum, deviation } from "d3-array";

/**
 * Renders summary statistics for a numeric array as a row of cards.
 * Each card shows a stat name and value.
 *
 * Use this in report nodes alongside (or instead of) charts to give
 * users a quick numeric summary of the data being analyzed.
 *
 * Example:
 *   <DescriptiveStats
 *     values={[3, 5, 4, 7, 12, 18, 22, 31, 27]}
 *     stats={["count", "mean", "median", "max"]}
 *   />
 */

type StatKey = "count" | "sum" | "mean" | "median" | "min" | "max" | "stddev";

export interface DescriptiveStatsProps {
  values: number[];
  /** Which stats to show, in order. Default: count, mean, median, min, max. */
  stats?: StatKey[];
  /** Number formatter. Defaults to compact locale string. */
  format?: (n: number) => string;
}

const DEFAULT_STATS: StatKey[] = ["count", "mean", "median", "min", "max"];

const STAT_LABELS: Record<StatKey, string> = {
  count: "n",
  sum: "sum",
  mean: "mean",
  median: "median",
  min: "min",
  max: "max",
  stddev: "std dev",
};

export function DescriptiveStats({
  values,
  stats = DEFAULT_STATS,
  format,
}: DescriptiveStatsProps) {
  const computed = useMemo(() => {
    if (values.length === 0) return null;
    return {
      count: values.length,
      sum: sum(values) ?? 0,
      mean: mean(values) ?? 0,
      median: median(values) ?? 0,
      min: min(values) ?? 0,
      max: max(values) ?? 0,
      stddev: deviation(values) ?? 0,
    };
  }, [values]);

  if (!computed) {
    return (
      <p className="text-sm text-muted italic">No data to summarize.</p>
    );
  }

  const fmt = format ?? defaultFormat;

  return (
    <div
      className="grid gap-px bg-border"
      style={{
        gridTemplateColumns: `repeat(${stats.length}, minmax(0, 1fr))`,
      }}
    >
      {stats.map((stat) => (
        <div
          key={stat}
          className="bg-surface px-3 py-3 flex flex-col gap-1"
        >
          <span className="text-[10px] uppercase tracking-[0.2em] text-muted font-mono">
            {STAT_LABELS[stat]}
          </span>
          <span className="font-mono text-base text-ink">
            {fmt(computed[stat])}
          </span>
        </div>
      ))}
    </div>
  );
}

function defaultFormat(n: number): string {
  if (!Number.isFinite(n)) return "—";
  if (Math.abs(n) >= 10000 || (Math.abs(n) < 0.01 && n !== 0)) {
    return n.toExponential(2);
  }
  if (Number.isInteger(n)) return n.toLocaleString();
  return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
}
