/**
 * Ordering for a histogram's bars.
 *
 * Pure, and on its own, because the bug it fixes is invisible by
 * inspection: `Object.entries` does NOT return insertion order. JavaScript
 * lists canonical non-negative integer keys first, in ascending numeric
 * order, and everything else after them. So a numeric axis containing any
 * negative value comes out reordered —
 *
 *     {"-2000": 1, "-200": 1, "2": 2, "700000": 1}
 *       → 2, 700000, -2000, -200
 *
 * — which puts the smallest value LAST. Selecting the whole range then
 * yields bounds of `start: 2, end: -200`, and a filter of
 * `>= 2 AND <= -200` matches nothing at all.
 */

import { type HistogramDatum } from "../charts";

export function orderHistogramData(
  raw: Record<string, number> | undefined,
): HistogramDatum[] {
  if (!raw) return [];

  const entries = Object.entries(raw).map(([label, count]) => ({
    label,
    count,
  }));

  // Only a numeric axis is reordered. Dates and categories keep the
  // order they arrived in: JavaScript preserves insertion order for
  // non-integer-like keys, so that order is the caller's, and the
  // caller may well mean something by it.
  const numeric =
    entries.length > 0 &&
    entries.every(
      (e) => e.label.trim() !== "" && Number.isFinite(Number(e.label)),
    );
  if (!numeric) return entries;

  return entries.sort((a, b) => Number(a.label) - Number(b.label));
}
