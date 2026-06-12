/**
 * Pure-functional utilities for the redistribution editor.
 *
 * The widget's UI logic boils down to three computations:
 *   1. Apply an operation: turn (sources, destinations, fraction,
 *      shape) into per-source-bucket allocations and fold them into
 *      the running mapping.
 *   2. Recompute the mapping from the full operations list. The
 *      mapping is derived state — `operations` is the authority,
 *      `mapping` is computed from it. Undo/reset both work by
 *      mutating `operations` and recomputing.
 *   3. Compute the preview histogram counts: each source bucket
 *      shrinks by the fraction redistributed elsewhere; each
 *      destination grows by the inflows.
 *
 * All three are pure functions on plain JSON-serializable data,
 * so they're trivially unit-testable without React. The widget
 * component pulls them in and renders the result.
 */

/** Histogram bucket. Caller-supplied order is preserved. */
export interface Bucket {
  key: string;
  count: number;
}

export type Policy =
  | "spread_even"
  | "match_shape"
  | "push_to_nearest"
  | "manual"
  | "drop";
export type Shape = "even" | "match";

/** A single user-initiated redistribution action. */
export interface Operation {
  sources: string[];
  destinations: string[];
  /** 0..1, the share of records taken from each source. */
  fraction: number;
  /** How to apportion across multiple destinations. */
  shape: Shape;
}

/**
 * The persisted value of the widget. `mapping` is derived from
 * `operations`; we ship both so consumers can read the result
 * directly without re-running the reducer.
 */
export interface RedistributionValue {
  policy: Policy;
  operations: Operation[];
  /** {source_bucket: {dest_bucket | "_dropped": fraction}} */
  mapping: Record<string, Record<string, number>>;
}

/** Marker key used inside a source's mapping to indicate dropped. */
export const DROPPED = "_dropped";

/**
 * Normalize `data` into an array of buckets in the order received.
 * Accepts either the canonical list-of-dicts form or a flat dict
 * (which loses ordering — caller is on their own if they pass a
 * dict and expect a meaningful order).
 */
export function normalizeData(
  raw: unknown,
): Bucket[] {
  if (Array.isArray(raw)) {
    // List-of-dicts: ordered, validated shape.
    return raw
      .map((r) => {
        if (!r || typeof r !== "object") return null;
        const o = r as Record<string, unknown>;
        const key = typeof o.key === "string" ? o.key : null;
        const count = typeof o.count === "number" ? o.count : null;
        if (key === null || count === null) return null;
        return { key, count };
      })
      .filter((b): b is Bucket => b !== null);
  }
  if (raw && typeof raw === "object") {
    // Flat dict — Object.entries order in modern engines is insertion
    // order for string keys, which works for our typical caller
    // (a backend that JSON-encodes a Python dict). Not a guarantee.
    return Object.entries(raw as Record<string, unknown>)
      .map(([key, count]) =>
        typeof count === "number" ? { key, count } : null,
      )
      .filter((b): b is Bucket => b !== null);
  }
  return [];
}

/**
 * Compute the per-source allocation a single operation produces.
 * Returns `{src: {dest: fraction}}` — fractions relative to the
 * source's own count, ready to fold into the running mapping.
 *
 * "Match" shape weights destinations by their **original** counts
 * — order-independent and matches what the user sees as the "shape"
 * of the data. "Even" weights them equally.
 *
 * The fraction "_dropped" appears when policy=drop or when the
 * operation's destinations are empty (the user is dropping the
 * sources rather than redistributing). The caller decides which
 * by leaving destinations empty.
 */
export function operationAllocations(
  op: Operation,
  originalCounts: Record<string, number>,
): Record<string, Record<string, number>> {
  const out: Record<string, Record<string, number>> = {};
  const { sources, destinations, fraction, shape } = op;
  // Per-destination weight derived from original counts under
  // "match", uniform under "even". An empty destinations list
  // means the operation is a drop — apportion `fraction` to
  // _dropped and stop.
  if (destinations.length === 0) {
    for (const s of sources) {
      out[s] = { [DROPPED]: fraction };
    }
    return out;
  }
  let weights: Record<string, number>;
  if (shape === "even") {
    const w = 1 / destinations.length;
    weights = Object.fromEntries(destinations.map((d) => [d, w]));
  } else {
    // "match": proportional to destinations' original counts.
    const sum = destinations.reduce(
      (acc, d) => acc + (originalCounts[d] ?? 0),
      0,
    );
    if (sum === 0) {
      // All destinations have zero count — fall back to even
      // (otherwise everything ends up at NaN).
      const w = 1 / destinations.length;
      weights = Object.fromEntries(destinations.map((d) => [d, w]));
    } else {
      weights = Object.fromEntries(
        destinations.map((d) => [d, (originalCounts[d] ?? 0) / sum]),
      );
    }
  }
  for (const s of sources) {
    const alloc: Record<string, number> = {};
    for (const d of destinations) {
      alloc[d] = fraction * weights[d];
    }
    out[s] = alloc;
  }
  return out;
}

/**
 * Fold the operations list into a mapping. The mapping per source
 * bucket sums up to at most 1.0; the implicit remainder is left
 * for future operations or final drop.
 *
 * When two operations touch the same source, allocations add. If
 * the total would exceed 1.0 (which the widget UI prevents but
 * we tolerate defensively here), the per-destination shares
 * for the offending operations get clipped so the per-source sum
 * lands at exactly 1.0.
 */
export function computeMapping(
  operations: Operation[],
  originalCounts: Record<string, number>,
): Record<string, Record<string, number>> {
  const mapping: Record<string, Record<string, number>> = {};
  for (const op of operations) {
    const alloc = operationAllocations(op, originalCounts);
    for (const [src, dests] of Object.entries(alloc)) {
      const cur = mapping[src] ?? {};
      // Sum used by `src` so far (across all prior ops).
      const used = Object.values(cur).reduce((a, b) => a + b, 0);
      // Clipping headroom — never exceed 1.0 per source.
      const headroom = Math.max(0, 1 - used);
      const totalThisOp = Object.values(dests).reduce((a, b) => a + b, 0);
      if (totalThisOp === 0) continue;
      const scale =
        totalThisOp > headroom ? headroom / totalThisOp : 1;
      const next = { ...cur };
      for (const [d, frac] of Object.entries(dests)) {
        const adj = frac * scale;
        if (adj === 0) continue;
        next[d] = (next[d] ?? 0) + adj;
      }
      mapping[src] = next;
    }
  }
  // Strip zero-valued entries to keep the persisted mapping tidy.
  for (const src of Object.keys(mapping)) {
    for (const d of Object.keys(mapping[src])) {
      if (mapping[src][d] === 0) delete mapping[src][d];
    }
    if (Object.keys(mapping[src]).length === 0) delete mapping[src];
  }
  return mapping;
}

/**
 * Compute the preview histogram counts after applying the mapping.
 * Each source bucket loses `count * sum(fractions)` records; each
 * destination gains `sum_over_sources(source_count * fraction)`.
 * `_dropped` removes records from the system entirely.
 *
 * Returns the updated count for every bucket key in `originalCounts`.
 * Non-source/non-destination buckets pass through unchanged.
 */
export function computePreview(
  originalCounts: Record<string, number>,
  mapping: Record<string, Record<string, number>>,
): Record<string, number> {
  const out: Record<string, number> = { ...originalCounts };
  for (const [src, dests] of Object.entries(mapping)) {
    const srcCount = originalCounts[src] ?? 0;
    const totalOut = Object.values(dests).reduce((a, b) => a + b, 0);
    out[src] = srcCount * (1 - totalOut);
    for (const [d, frac] of Object.entries(dests)) {
      if (d === DROPPED) continue;
      out[d] = (out[d] ?? 0) + srcCount * frac;
    }
  }
  return out;
}

/**
 * Count of source-bucket records that the user hasn't allocated yet
 * — the "47 records waiting to place" UI counter. Equals total
 * source records minus what the mapping accounts for (redistributed
 * or explicitly dropped).
 *
 * In `policy=drop` mode this returns 0 regardless of the operations
 * list — drop policy implicitly drops every source record.
 */
export function unallocatedCount(
  sources: string[],
  originalCounts: Record<string, number>,
  mapping: Record<string, Record<string, number>>,
  policy: Policy,
): number {
  if (policy === "drop") return 0;
  let total = 0;
  for (const s of sources) {
    const srcCount = originalCounts[s] ?? 0;
    const used = Object.values(mapping[s] ?? {}).reduce(
      (a, b) => a + b,
      0,
    );
    total += srcCount * (1 - used);
  }
  // Floating-point dust can leave a small non-zero residue at full
  // allocation — round to the nearest record for the UI counter.
  return Math.max(0, Math.round(total));
}

/**
 * Validation for the persisted value. Returns null if valid, or a
 * message describing the first failure. Called by the widget bundle's
 * `validate` hook before form submission.
 *
 * Rules:
 *   - `policy` is one of "redistribute", "drop".
 *   - `mapping` keys are all in `sources`; inner keys are all in
 *     `destinations` or the reserved DROPPED marker.
 *   - In `redistribute` policy, no source's mapping may rely on
 *     DROPPED (drop is its own policy).
 *   - Per-source fractions sum to <= 1.0 within tolerance.
 */
export function validateValue(
  value: RedistributionValue | undefined,
  sources: string[],
  destinations: string[],
): string | null {
  if (!value) return null; // optional case is fine; required handled separately.
  const validPolicies = new Set<Policy>([
    "spread_even",
    "match_shape",
    "push_to_nearest",
    "manual",
    "drop",
  ]);
  if (!validPolicies.has(value.policy as Policy)) {
    return `invalid policy: ${value.policy}`;
  }
  const sourceSet = new Set(sources);
  const destSet = new Set(destinations);
  for (const [src, dests] of Object.entries(value.mapping ?? {})) {
    if (!sourceSet.has(src)) {
      return `mapping references unknown source bucket: ${src}`;
    }
    let total = 0;
    for (const [d, frac] of Object.entries(dests)) {
      if (d !== DROPPED && !destSet.has(d)) {
        return `mapping references unknown destination bucket: ${d}`;
      }
      if (value.policy !== "manual" && d === DROPPED) {
        // The `_dropped` marker is only meaningful when `manual`
        // operations express it OR when policy is `drop` (which
        // emits an all-dropped mapping). For other deterministic
        // policies (spread_even, match_shape, push_to_nearest),
        // dropping isn't possible — flag it.
        if (value.policy !== "drop") {
          return (
            `policy is "${value.policy}" but source ${src} has a ` +
            `_dropped allocation — only the "drop" or "manual" ` +
            `policies can emit _dropped`
          );
        }
      }
      total += frac;
    }
    if (total > 1.0001) {
      return (
        `source ${src} has allocations summing to ` +
        `${total.toFixed(3)} (must be ≤ 1.0)`
      );
    }
  }
  return null;
}

// --- Policy strategies ------------------------------------------------
//
// One function per non-manual policy. Each takes the sources,
// destinations, and a counts table and returns a mapping
// deterministically. No operations list involved; the mapping IS
// the strategy's complete output.
//
// For `manual` policy, use `computeMapping(operations, counts)`
// instead — operations are the source of truth there.

/**
 * "spread_even" — every source bucket distributes evenly across
 * every destination bucket. If a source has N records and there
 * are M destinations, each destination receives N/M of those
 * records.
 */
export function strategySpreadEven(
  sources: string[],
  destinations: string[],
): Record<string, Record<string, number>> {
  if (sources.length === 0 || destinations.length === 0) return {};
  const weight = 1 / destinations.length;
  const destFracs: Record<string, number> = {};
  for (const d of destinations) destFracs[d] = weight;
  const out: Record<string, Record<string, number>> = {};
  for (const s of sources) out[s] = { ...destFracs };
  return out;
}

/**
 * "match_shape" — every source bucket distributes across
 * destinations weighted by destinations' *original* counts. The
 * shape of the destination histogram is preserved (scaled up by
 * the inflowing records).
 *
 * All-zero destinations fall back to even — otherwise the math
 * divides by zero. Same defensive behavior as the per-operation
 * "match" shape inside operationAllocations.
 */
export function strategyMatchShape(
  sources: string[],
  destinations: string[],
  originalCounts: Record<string, number>,
): Record<string, Record<string, number>> {
  if (sources.length === 0 || destinations.length === 0) return {};
  const totalDestCount = destinations.reduce(
    (acc, d) => acc + (originalCounts[d] ?? 0),
    0,
  );
  if (totalDestCount === 0) {
    return strategySpreadEven(sources, destinations);
  }
  const destFracs: Record<string, number> = {};
  for (const d of destinations) {
    destFracs[d] = (originalCounts[d] ?? 0) / totalDestCount;
  }
  const out: Record<string, Record<string, number>> = {};
  for (const s of sources) out[s] = { ...destFracs };
  return out;
}

/**
 * "push_to_nearest" — each source bucket sends 100% to its nearest
 * destination by position in `dataOrder`. The caller passes `data`
 * in meaningful order (the same order the histogram renders), so
 * "nearest by index" matches "nearest on the axis."
 *
 * Ties (two destinations equidistant from a source) resolve to
 * the earlier-indexed destination — stable and predictable.
 * Sources without any reachable destinations are dropped from the
 * mapping; the widget renders the resulting empty allocation
 * clearly via the counter.
 */
export function strategyPushToNearest(
  sources: string[],
  destinations: string[],
  dataOrder: string[],
): Record<string, Record<string, number>> {
  if (sources.length === 0 || destinations.length === 0) return {};
  // Pre-compute destination indices for O(1) distance lookup per
  // source. A destination not in dataOrder is silently skipped.
  const destIdx: Array<[string, number]> = [];
  for (const d of destinations) {
    const idx = dataOrder.indexOf(d);
    if (idx !== -1) destIdx.push([d, idx]);
  }
  if (destIdx.length === 0) return {};
  const out: Record<string, Record<string, number>> = {};
  for (const src of sources) {
    const srcIdx = dataOrder.indexOf(src);
    if (srcIdx === -1) continue;
    let bestKey = destIdx[0][0];
    let bestDist = Math.abs(destIdx[0][1] - srcIdx);
    for (let i = 1; i < destIdx.length; i++) {
      const dist = Math.abs(destIdx[i][1] - srcIdx);
      if (dist < bestDist) {
        bestDist = dist;
        bestKey = destIdx[i][0];
      }
    }
    out[src] = { [bestKey]: 1.0 };
  }
  return out;
}

/**
 * "drop" — discard every source record. Every source maps fully
 * to the reserved `_dropped` marker. The form-side helper drops
 * those rows when applying the mapping.
 */
export function strategyDropAll(
  sources: string[],
): Record<string, Record<string, number>> {
  const out: Record<string, Record<string, number>> = {};
  for (const s of sources) out[s] = { [DROPPED]: 1.0 };
  return out;
}

/**
 * Dispatch the active policy to its strategy. `manual` uses the
 * operations-based `computeMapping`; everything else is
 * deterministic from sources / destinations / data / original
 * counts.
 *
 * Single entry point the widget uses to recompute the mapping any
 * time the user toggles policies or commits an operation. Keeping
 * it centralized means the policy↔mapping relationship is defined
 * once and exhaustively type-checked (Policy is a union; the
 * switch is exhaustive).
 */
export function computePolicyMapping(
  policy: Policy,
  sources: string[],
  destinations: string[],
  dataOrder: string[],
  originalCounts: Record<string, number>,
  operations: Operation[],
): Record<string, Record<string, number>> {
  switch (policy) {
    case "spread_even":
      return strategySpreadEven(sources, destinations);
    case "match_shape":
      return strategyMatchShape(sources, destinations, originalCounts);
    case "push_to_nearest":
      return strategyPushToNearest(sources, destinations, dataOrder);
    case "drop":
      return strategyDropAll(sources);
    case "manual":
      return computeMapping(operations, originalCounts);
  }
}
