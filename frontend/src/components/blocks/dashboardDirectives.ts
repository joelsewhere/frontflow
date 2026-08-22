/**
 * Matching and translation for dashboard directives.
 *
 * Pure — no React, no DOM, no dockview — so it can be transpiled and
 * run directly. That matters here because the rules are easy to state
 * and easy to get subtly wrong: which panel a directive addresses, and
 * how a filter NAME becomes the id and column Superset's data mask
 * wants.
 *
 *   npx esbuild src/components/blocks/dashboardDirectives.ts \
 *       --format=cjs --outfile=/tmp/d.cjs
 *   DIRECTIVES_BUNDLE=/tmp/d.cjs node src/components/blocks/dashboardDirectives.test.cjs
 */

import type { DashboardFilter, DashboardFilterDirective } from "../../lib/api";

/**
 * The most recent filter directive for `name` in this submission.
 *
 * Same rule as a refresh: tokens strictly increase, so the maximum is
 * the newest, and only the newest matters.
 */
export function latestFilterDirectiveFor(
  tasks: { dashboard_filters?: DashboardFilterDirective | null }[] | undefined,
  name: string,
  panelId: string | null,
) {
  if (!tasks) return null;
  const matching = tasks
    .map((t) => t.dashboard_filters)
    .filter(
      (d): d is DashboardFilterDirective =>
        Boolean(d) &&
        d!.dashboard === name &&
        // An unnamed panel addresses every rendering; a named one only
        // the rendering that carries that id.
        (!d!.panel || d!.panel === panelId),
    );
  if (matching.length === 0) return null;
  return matching.reduce((a, b) => (a.token >= b.token ? a : b));
}

/**
 * Named filter values, as a Superset data mask.
 *
 * The directive names filters the way the author named them in
 * Superset; the mask is keyed by filter id and carries the target
 * column. Matching is case-insensitive on the name, because "Region" on
 * a filter bar and `region=` in a workflow are plainly the same thing
 * and failing over the capital would be pedantry.
 *
 * A filter the dashboard does not have is skipped rather than guessed
 * at — applying the wrong filter is worse than applying none.
 */
export function buildFilterMask(
  values: Record<string, string | string[]>,
  available: DashboardFilter[],
): Record<string, unknown> {
  const byName = new Map(
    available.map((f) => [f.name.trim().toLowerCase(), f]),
  );

  const mask: Record<string, unknown> = {};
  for (const [rawName, rawValue] of Object.entries(values)) {
    const filter = byName.get(rawName.trim().toLowerCase());
    if (!filter || !filter.id) continue;

    if (filter.is_time) {
      // A time filter takes a range, not a set of values.
      const range = Array.isArray(rawValue) ? rawValue[0] : rawValue;
      mask[filter.id] = {
        extraFormData: { time_range: range },
        filterState: { value: range, label: range },
      };
      continue;
    }

    if (!filter.column) continue;
    const list = Array.isArray(rawValue) ? rawValue : [rawValue];
    mask[filter.id] = {
      extraFormData: {
        filters: [{ col: filter.column, op: "IN", val: list }],
      },
      filterState: { value: list, label: list.join(", ") },
    };
  }
  return mask;
}

