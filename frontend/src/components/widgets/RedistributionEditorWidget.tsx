import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { Histogram, type HistogramScales } from "../charts";
import { type Widget, type WidgetProps } from "./types";
import {
  computePolicyMapping,
  computePreview,
  normalizeData,
  operationAllocations,
  unallocatedCount,
  validateValue,
  type Bucket,
  type Operation,
  type Policy,
  type RedistributionValue,
  type Shape,
} from "./redistribution/state";

/**
 * Redistribution editor widget.
 *
 * Renders an editable histogram split into "source" buckets (red —
 * need to be redistributed) and "destination" buckets (gray —
 * candidates to receive). The user brushes within either color to
 * build selections, picks a fraction + shape, and commits a single
 * operation. The histogram updates live. An operations list tracks
 * each commit for undo. The persisted value carries both the
 * operations (audit) and the derived mapping (consumable result).
 *
 * Architecture:
 *   - `state.ts` holds the pure math (reducer + preview + validation).
 *     Unit-testable without React. This file is the React glue.
 *   - Selection state is component-local — operations are the source
 *     of truth; selections are transient UI affordances.
 *   - The Histogram chart component is reused as-is; its
 *     `renderOverlay` prop layers selection rectangles on top.
 */
export interface RedistributionWidgetField {
  name: string;
  label: string;
  required: boolean;
  widget_data?: {
    xcom_key?: string;
    policies?: Policy[];
    default_policy?: Policy;
    value_label?: string;
  };
}

// --- Geometry constants. Match DistributionFilter's defaults. ---
const CHART_H = 200;
const AXIS_H = 22;

// --- Bar colors. CSS variables resolve in the renderer. ---
const SOURCE_FILL = "rgb(var(--color-error))";
const DEST_FILL = "rgb(var(--color-muted))";
const NEUTRAL_FILL = "rgb(var(--color-border))";

type ActiveColor = "source" | "destination";

function RedistributionEditorComponent({
  field,
  xcom,
  value,
  onChange,
  error,
}: WidgetProps<RedistributionValue>) {
  // The block-tree wrapper packs data, sources, destinations into
  // `xcom` under the widget's id (see RedistributionWidgetBlock in
  // BlockTree.tsx). Read them out here.
  const xcomKey = (field.widget_data?.xcom_key as string | undefined) ?? "";
  const bundle = xcomKey
    ? (xcom[xcomKey] as
        | {
            data?: unknown;
            sources?: string[];
            destinations?: string[];
          }
        | undefined)
    : undefined;
  const policies =
    (field.widget_data?.policies as Policy[] | undefined) ?? [
      "spread_even",
      "match_shape",
      "push_to_nearest",
      "manual",
      "drop",
    ];
  const defaultPolicy =
    (field.widget_data?.default_policy as Policy | undefined) ??
    "manual";
  const valueLabel =
    (field.widget_data?.value_label as string | undefined) ?? "records";

  const data: Bucket[] = useMemo(
    () => normalizeData(bundle?.data),
    [bundle?.data],
  );
  const sources: string[] = useMemo(
    () => bundle?.sources ?? [],
    [bundle?.sources],
  );
  const destinations: string[] = useMemo(
    () => bundle?.destinations ?? [],
    [bundle?.destinations],
  );
  const originalCounts: Record<string, number> = useMemo(() => {
    const out: Record<string, number> = {};
    for (const b of data) out[b.key] = b.count;
    return out;
  }, [data]);

  // Role classification per bucket — drives bar fill + brush logic.
  const roleOf = useCallback(
    (key: string): ActiveColor | null => {
      if (sources.includes(key)) return "source";
      if (destinations.includes(key)) return "destination";
      return null;
    },
    [sources, destinations],
  );

  // --- Seed value on first render -----------------------------------
  // Form state arrives undefined on first render. Seed with an empty
  // value so downstream reads (preview, counter) don't have to keep
  // null-checking.
  const seededRef = useRef(false);
  useEffect(() => {
    if (seededRef.current) return;
    if (data.length === 0) return;
    if (!value) {
      const initialMapping = computePolicyMapping(
        defaultPolicy,
        sources,
        destinations,
        data.map((b) => b.key),
        Object.fromEntries(data.map((b) => [b.key, b.count])),
        [],
      );
      onChange({
        policy: defaultPolicy,
        operations: [],
        mapping: initialMapping,
      });
    }
    seededRef.current = true;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data.length]);

  // Resolved value used by render — falls back to a synthetic empty.
  const v: RedistributionValue = value ?? {
    policy: defaultPolicy,
    operations: [],
    mapping: {},
  };
  const policy = v.policy;
  const operations = v.operations;
  const mapping = v.mapping;

  // --- Preview --------------------------------------------------------
  const previewCounts = useMemo(
    () => computePreview(originalCounts, mapping),
    [originalCounts, mapping],
  );
  const previewBuckets: Bucket[] = useMemo(
    () => data.map((b) => ({ key: b.key, count: previewCounts[b.key] ?? 0 })),
    [data, previewCounts],
  );
  // The shared Histogram chart speaks `{label, count}`; our internal
  // contract uses `{key, count}`. Adapt at the boundary so state.ts
  // stays consistent with the data contract we ship to backends.
  const histogramData = useMemo(
    () => previewBuckets.map((b) => ({ label: b.key, count: b.count })),
    [previewBuckets],
  );

  // Records the user still has to allocate.
  const remaining = useMemo(
    () => unallocatedCount(sources, originalCounts, mapping, policy),
    [sources, originalCounts, mapping, policy],
  );
  // Total source records — denominator for the "X of Y waiting" line.
  const totalSourceRecords = useMemo(
    () => sources.reduce((acc, s) => acc + (originalCounts[s] ?? 0), 0),
    [sources, originalCounts],
  );

  // --- Operation-builder local state ---------------------------------
  const [sourceSel, setSourceSel] = useState<Set<string>>(new Set());
  const [destSel, setDestSel] = useState<Set<string>>(new Set());
  const [fractionPct, setFractionPct] = useState(100); // 0..100, user-friendly
  const [shape, setShape] = useState<Shape>("match");

  // Clear selections whenever the operations list changes — keeps the
  // builder fresh after each commit / undo / reset.
  useEffect(() => {
    setSourceSel(new Set());
    setDestSel(new Set());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [operations.length]);

  // --- Brush state ---------------------------------------------------
  // The brush is anchored on pointerdown at a bucket of a particular
  // color; the active color is then locked for the duration of the
  // drag. Bars of the other color are excluded from the selection.
  const svgRef = useRef<SVGSVGElement>(null);
  const scalesRef = useRef<HistogramScales | null>(null);
  type Brush = {
    active: ActiveColor;
    anchorIdx: number;
    currentIdx: number;
    // True when shift was held at brush start — selection adds to the
    // existing set instead of replacing it.
    additive: boolean;
  };
  const [brush, setBrush] = useState<Brush | null>(null);
  // Index of the bucket currently under the pointer — drives the
  // hover tooltip that surfaces original/preview counts and role.
  // Tracked separately from `brush` so the tooltip is live even
  // when the user isn't dragging.
  const [hoveredIdx, setHoveredIdx] = useState<number | null>(null);

  function bucketAt(clientX: number): number | null {
    if (!svgRef.current || !scalesRef.current) return null;
    const rect = svgRef.current.getBoundingClientRect();
    const x = clientX - rect.left;
    const idx = scalesRef.current.xToIndex(x);
    if (idx < 0 || idx >= data.length) return null;
    return idx;
  }

  function onPointerDown(e: ReactPointerEvent<SVGSVGElement>) {
    if (data.length === 0) return;
    const idx = bucketAt(e.clientX);
    if (idx === null) return;
    const role = roleOf(data[idx].key);
    if (role === null) return; // neutral bar: not selectable
    // Ctrl/Cmd-click: toggle a single bar without engaging a brush.
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      toggleOne(data[idx].key, role);
      return;
    }
    // Shift-click: extend the active selection of that color from
    // the most-recent anchor to this index.
    const additive = e.shiftKey;
    setBrush({
      active: role,
      anchorIdx: idx,
      currentIdx: idx,
      additive,
    });
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* noop */
    }
  }

  function onPointerMove(e: ReactPointerEvent<SVGSVGElement>) {
    const idx = bucketAt(e.clientX);
    setHoveredIdx(idx);
    if (!brush) return;
    if (idx === null) return;
    if (idx === brush.currentIdx) return;
    setBrush({ ...brush, currentIdx: idx });
  }

  function onPointerUp(e: ReactPointerEvent<SVGSVGElement>) {
    if (!brush) return;
    const [lo, hi] =
      brush.anchorIdx <= brush.currentIdx
        ? [brush.anchorIdx, brush.currentIdx]
        : [brush.currentIdx, brush.anchorIdx];
    // Filter to bars whose color matches the brush's active color.
    const picked = new Set<string>();
    for (let i = lo; i <= hi; i++) {
      const k = data[i].key;
      if (roleOf(k) === brush.active) picked.add(k);
    }
    commitSelection(brush.active, picked, brush.additive);
    setBrush(null);
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      /* noop */
    }
  }

  function toggleOne(key: string, color: ActiveColor) {
    if (color === "source") {
      setSourceSel((prev) => {
        const next = new Set(prev);
        if (next.has(key)) next.delete(key);
        else next.add(key);
        return next;
      });
    } else {
      setDestSel((prev) => {
        const next = new Set(prev);
        if (next.has(key)) next.delete(key);
        else next.add(key);
        return next;
      });
    }
  }

  function commitSelection(
    color: ActiveColor,
    picked: Set<string>,
    additive: boolean,
  ) {
    if (color === "source") {
      setSourceSel(
        additive ? new Set([...sourceSel, ...picked]) : picked,
      );
    } else {
      setDestSel(
        additive ? new Set([...destSel, ...picked]) : picked,
      );
    }
  }

  // --- Operation commit ----------------------------------------------
  // Apply is meaningful only in manual policy — the others compute
  // their mapping deterministically and don't take user operations.
  const canApply = useMemo(() => {
    if (policy !== "manual") return false;
    if (sourceSel.size === 0) return false;
    // In manual policy, destinations may be empty if "drop" is in
    // the allowed policies — that's an explicit per-operation drop.
    if (destSel.size === 0 && !policies.includes("drop")) return false;
    if (fractionPct <= 0) return false;
    return true;
  }, [policy, sourceSel.size, destSel.size, fractionPct, policies]);

  function applyOp() {
    if (!canApply) return;
    const op: Operation = {
      sources: Array.from(sourceSel),
      destinations: Array.from(destSel),
      fraction: fractionPct / 100,
      shape,
    };
    const newOps = [...operations, op];
    const newMapping = computePolicyMapping(
      "manual",
      sources,
      destinations,
      data.map((b) => b.key),
      originalCounts,
      newOps,
    );
    onChange({ policy, operations: newOps, mapping: newMapping });
  }

  // --- Undo + reset --------------------------------------------------
  function undo() {
    if (operations.length === 0) return;
    const newOps = operations.slice(0, -1);
    const newMapping = computePolicyMapping(
      policy,
      sources,
      destinations,
      data.map((b) => b.key),
      originalCounts,
      newOps,
    );
    onChange({ policy, operations: newOps, mapping: newMapping });
  }

  function reset() {
    // Reset clears manual operations. For the deterministic
    // policies, reset is a no-op (the mapping is fully determined
    // by the policy + inputs, not by stored state).
    const newMapping = computePolicyMapping(
      policy,
      sources,
      destinations,
      data.map((b) => b.key),
      originalCounts,
      [],
    );
    onChange({ policy, operations: [], mapping: newMapping });
  }

  // --- Policy switch -------------------------------------------------
  function switchPolicy(next: Policy) {
    if (next === policy) return;
    // Recompute mapping under the new policy. Each policy is
    // deterministic except `manual`, which derives from the
    // preserved operations list. Switching policies is non-
    // destructive — operations stick around in case the user
    // returns to manual.
    const newMapping = computePolicyMapping(
      next,
      sources,
      destinations,
      data.map((b) => b.key),
      originalCounts,
      operations,
    );
    onChange({ policy: next, operations, mapping: newMapping });
  }

  // --- Render --------------------------------------------------------
  if (data.length === 0) {
    return (
      <div className="border border-border bg-surface px-3 py-2 text-sm text-muted italic">
        No data to redistribute.
      </div>
    );
  }

  // Bar coloring: source/destination role, dimmed in drop policy.
  // The Histogram chart passes datums in its own `{label, count}`
  // shape — `label` is our `key` (set in the adapter just above).
  const barFill = (d: { label: string; count: number }) => {
    const role = roleOf(d.label);
    if (role === "source") return SOURCE_FILL;
    if (role === "destination") return DEST_FILL;
    return NEUTRAL_FILL;
  };

  const barOpacity = (d: { label: string; count: number }) => {
    const role = roleOf(d.label);
    if (role === null) return 0.35;
    const selected =
      (role === "source" && sourceSel.has(d.label)) ||
      (role === "destination" && destSel.has(d.label));
    return selected ? 0.95 : 0.5;
  };

  // Brush overlay — a translucent rectangle covering the dragged
  // range. Constrained to the brush's active color via stroke color.
  const brushRange = brush
    ? brush.anchorIdx <= brush.currentIdx
      ? [brush.anchorIdx, brush.currentIdx]
      : [brush.currentIdx, brush.anchorIdx]
    : null;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex justify-between items-end gap-4">
        <span className="font-mono text-xs uppercase tracking-[0.18em] text-muted">
          {field.label}
        </span>
        <PolicyToggle
          policies={policies}
          active={policy}
          onChange={switchPolicy}
        />
      </div>

      <div className="bg-surface border border-border px-1 pt-2 pb-1">
        <Histogram
          data={histogramData}
          height={CHART_H + AXIS_H}
          barFill={barFill}
          barOpacity={barOpacity}
          numTicks={5}
          disableTooltip={true}
          svgRef={svgRef}
          svgProps={{
            onPointerDown,
            onPointerMove,
            onPointerUp,
            onPointerLeave: () => {
              setBrush(null);
              setHoveredIdx(null);
            },
          }}
          renderOverlay={(scales) => {
            scalesRef.current = scales;
            if (!brushRange) return null;
            const [lo, hi] = brushRange;
            const x = scales.xOf(lo);
            const w = scales.xOf(hi) + scales.bandwidth - x;
            const strokeColor =
              brush?.active === "source" ? SOURCE_FILL : DEST_FILL;
            return (
              <rect
                x={x}
                y={4}
                width={w}
                height={CHART_H - 8}
                fill="rgb(var(--color-accent))"
                fillOpacity={0.08}
                stroke={strokeColor}
                strokeOpacity={0.4}
                strokeWidth={1}
                pointerEvents="none"
              />
            );
          }}
          renderHtmlOverlay={({ scales }) => {
            // Tooltip: hover-over-a-bar inspector. Suppressed during
            // active brush drag (the brush rectangle gives the same
            // information visually and the tooltip would compete).
            if (hoveredIdx === null || brush !== null) return null;
            const bucket = data[hoveredIdx];
            if (!bucket) return null;
            const orig = originalCounts[bucket.key] ?? 0;
            const previewVal = previewCounts[bucket.key] ?? 0;
            const role = roleOf(bucket.key);
            const delta = previewVal - orig;
            const yTop = scales.yScale(
              Math.min(orig, previewVal),
            ) ?? 0;
            return (
              <div
                className="absolute pointer-events-none bg-ink text-bg font-mono text-[11px] px-2 py-1 whitespace-nowrap shadow-sm"
                style={{
                  left: `${4 + scales.xOf(hoveredIdx) + scales.bandwidth / 2}px`,
                  top: `${8 + yTop}px`,
                  transform: "translate(-50%, calc(-100% - 6px))",
                }}
              >
                <div>{bucket.key}</div>
                <div className="opacity-70">
                  {orig.toLocaleString()} {valueLabel}
                  {role !== null
                    ? ` · ${role}`
                    : ""}
                </div>
                {delta !== 0 ? (
                  <div className="opacity-90 mt-0.5 border-t border-bg/30 pt-0.5">
                    → {previewVal.toLocaleString()}
                    {" "}
                    <span className={delta > 0 ? "text-accent" : "opacity-80"}>
                      ({delta > 0 ? "+" : ""}
                      {delta.toLocaleString()})
                    </span>
                  </div>
                ) : null}
              </div>
            );
          }}
        />
      </div>

      <Legend />

      {/*
        Active-area UI dispatches on the current policy:
          - manual: operation builder + operations list + counter
          - drop: one-line summary (everything goes to /dev/null)
          - others: one-line summary of the deterministic strategy

        The histogram preview above already reflects whichever
        policy is active, so the user can see the consequence at
        a glance regardless of which area is rendered below.
      */}
      {policy === "manual" ? (
        <>
          <Counter
            remaining={remaining}
            total={totalSourceRecords}
            valueLabel={valueLabel}
          />
          <OperationBuilder
            sourceSel={sourceSel}
            destSel={destSel}
            fractionPct={fractionPct}
            shape={shape}
            canApply={canApply}
            policiesIncludeDrop={policies.includes("drop")}
            valueLabel={valueLabel}
            onClearSource={() => setSourceSel(new Set())}
            onClearDest={() => setDestSel(new Set())}
            onFractionChange={setFractionPct}
            onShapeChange={setShape}
            onApply={applyOp}
          />
          <OperationsList
            operations={operations}
            onUndo={undo}
            onReset={reset}
          />
        </>
      ) : (
        <PolicySummary
          policy={policy}
          totalSourceRecords={totalSourceRecords}
          destinationCount={destinations.length}
          valueLabel={valueLabel}
        />
      )}

      {error ? <p className="text-xs text-error">{error}</p> : null}
    </div>
  );
}

// --- Subcomponents ---------------------------------------------------

function PolicyToggle({
  policies,
  active,
  onChange,
}: {
  policies: Policy[];
  active: Policy;
  onChange: (p: Policy) => void;
}) {
  if (policies.length < 2) return null;
  return (
    <div className="inline-flex border border-border">
      {policies.map((p) => (
        <button
          key={p}
          type="button"
          onClick={() => onChange(p)}
          className={[
            "px-3 py-1 font-mono text-[10px] uppercase tracking-[0.18em]",
            "transition-colors",
            active === p
              ? "bg-ink text-bg"
              : "bg-surface text-muted hover:text-ink",
          ].join(" ")}
        >
          {p}
        </button>
      ))}
    </div>
  );
}

function Legend() {
  return (
    <div className="flex gap-4 text-[10px] font-mono uppercase tracking-[0.18em] text-muted">
      <span className="inline-flex items-center gap-1.5">
        <span
          className="inline-block w-3 h-3"
          style={{ backgroundColor: SOURCE_FILL, opacity: 0.7 }}
        />
        Source
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span
          className="inline-block w-3 h-3"
          style={{ backgroundColor: DEST_FILL, opacity: 0.7 }}
        />
        Destination
      </span>
      <span className="ml-auto text-muted/70 normal-case">
        Drag in chart to select · Ctrl-click toggles · Shift-click extends
      </span>
    </div>
  );
}

function Counter({
  remaining,
  total,
  valueLabel,
}: {
  remaining: number;
  total: number;
  valueLabel: string;
}) {
  const done = total - remaining;
  return (
    <p className="font-mono text-xs text-muted">
      <span
        className={remaining === 0 ? "text-accent" : "text-ink"}
      >
        {done.toLocaleString()} / {total.toLocaleString()} {valueLabel}
      </span>{" "}
      allocated
      {remaining > 0
        ? ` · ${remaining.toLocaleString()} waiting to place`
        : " · ready"}
    </p>
  );
}

function PolicySummary({
  policy,
  totalSourceRecords,
  destinationCount,
  valueLabel,
}: {
  policy: Policy;
  totalSourceRecords: number;
  destinationCount: number;
  valueLabel: string;
}) {
  // One-line description of what the active deterministic policy
  // is doing. The histogram above already shows the consequence
  // — this just labels it in words. `manual` never reaches this
  // (the operation builder renders instead); included for
  // exhaustiveness.
  let text: string;
  switch (policy) {
    case "spread_even":
      text =
        `Spreading ${totalSourceRecords.toLocaleString()} ${valueLabel} ` +
        `evenly across ${destinationCount.toLocaleString()} ` +
        `destination${destinationCount === 1 ? "" : "s"}` +
        ` (${(totalSourceRecords / Math.max(1, destinationCount)).toFixed(1)} each on average).`;
      break;
    case "match_shape":
      text =
        `Distributing ${totalSourceRecords.toLocaleString()} ${valueLabel} ` +
        `weighted by destination counts — preserves the ` +
        `destination histogram shape.`;
      break;
    case "push_to_nearest":
      text =
        `Each source bucket sends its ${valueLabel} 100% to its ` +
        `nearest destination by position on the axis.`;
      break;
    case "drop":
      text =
        `Discarding all ${totalSourceRecords.toLocaleString()} ` +
        `${valueLabel} from source buckets.`;
      break;
    case "manual":
      text = "Manual policy — use the operation builder below.";
      break;
  }
  return (
    <p className="text-xs text-muted border-l-2 border-border pl-2">
      {text}
    </p>
  );
}

function OperationBuilder({
  sourceSel,
  destSel,
  fractionPct,
  shape,
  canApply,
  policiesIncludeDrop,
  valueLabel,
  onClearSource,
  onClearDest,
  onFractionChange,
  onShapeChange,
  onApply,
}: {
  sourceSel: Set<string>;
  destSel: Set<string>;
  fractionPct: number;
  shape: Shape;
  canApply: boolean;
  policiesIncludeDrop: boolean;
  valueLabel: string;
  onClearSource: () => void;
  onClearDest: () => void;
  onFractionChange: (n: number) => void;
  onShapeChange: (s: Shape) => void;
  onApply: () => void;
}) {
  return (
    <div className="border border-border bg-surface p-3 flex flex-col gap-3">
      <div className="grid grid-cols-2 gap-3">
        <SelectionSummary
          label="Sources"
          color={SOURCE_FILL}
          selection={sourceSel}
          onClear={onClearSource}
        />
        <SelectionSummary
          label="Destinations"
          color={DEST_FILL}
          selection={destSel}
          onClear={onClearDest}
          // When destinations is empty, show a hint that the op
          // will act as a per-operation drop — only meaningful if
          // "drop" is in the policies allow-list.
          emptyHint={
            policiesIncludeDrop
              ? `(empty = drop these ${valueLabel})`
              : `Pick at least one destination`
          }
        />
      </div>

      <div className="flex items-center gap-4 text-xs">
        <label className="inline-flex items-center gap-2 text-ink">
          <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted">
            Send
          </span>
          <input
            type="number"
            min={1}
            max={100}
            step={1}
            value={fractionPct}
            onChange={(e) => {
              const n = Number(e.target.value);
              if (Number.isNaN(n)) return;
              onFractionChange(Math.min(100, Math.max(1, Math.round(n))));
            }}
            className="w-16 border border-border bg-bg px-2 py-1 text-right font-mono text-sm"
          />
          <span className="font-mono text-sm text-muted">%</span>
        </label>

        <label className="inline-flex items-center gap-2 text-ink">
          <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted">
            Shape
          </span>
          <select
            value={shape}
            onChange={(e) => onShapeChange(e.target.value as Shape)}
            className="border border-border bg-bg px-2 py-1 font-mono text-sm text-ink"
          >
            <option value="match">match original shape</option>
            <option value="even">even split</option>
          </select>
        </label>

        <button
          type="button"
          disabled={!canApply}
          onClick={onApply}
          className={[
            "ml-auto px-3 py-1.5 font-mono text-[10px] uppercase tracking-[0.18em]",
            "border transition-colors",
            canApply
              ? "border-ink bg-ink text-bg hover:bg-accent-hover"
              : "border-border bg-surface text-muted/60 cursor-not-allowed",
          ].join(" ")}
        >
          Distribute
        </button>
      </div>
    </div>
  );
}

function SelectionSummary({
  label,
  color,
  selection,
  onClear,
  emptyHint,
}: {
  label: string;
  color: string;
  selection: Set<string>;
  onClear: () => void;
  emptyHint?: string;
}) {
  const items = Array.from(selection);
  return (
    <div className="flex flex-col gap-1">
      <div className="flex justify-between items-center">
        <span className="inline-flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.18em] text-muted">
          <span
            className="inline-block w-2 h-2"
            style={{ backgroundColor: color }}
          />
          {label}
        </span>
        {items.length > 0 ? (
          <button
            type="button"
            onClick={onClear}
            className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted hover:text-error"
          >
            Clear
          </button>
        ) : null}
      </div>
      {items.length === 0 ? (
        <span className="text-xs italic text-muted/70">
          {emptyHint ?? "Brush in chart to select"}
        </span>
      ) : (
        <span className="text-xs text-ink break-all">
          {items.length} selected
          {items.length <= 5
            ? `: ${items.join(", ")}`
            : ` (${items.slice(0, 3).join(", ")}, … +${items.length - 3})`}
        </span>
      )}
    </div>
  );
}

function OperationsList({
  operations,
  onUndo,
  onReset,
}: {
  operations: Operation[];
  onUndo: () => void;
  onReset: () => void;
}) {
  if (operations.length === 0) {
    return (
      <p className="text-xs italic text-muted/70">
        No operations yet. Brush in the chart to select buckets,
        then click Distribute.
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      <div className="flex justify-between items-center">
        <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted">
          Operations
        </span>
        <div className="flex gap-3">
          <button
            type="button"
            onClick={onUndo}
            className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted hover:text-ink"
          >
            Undo
          </button>
          <button
            type="button"
            onClick={onReset}
            className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted hover:text-error"
          >
            Reset
          </button>
        </div>
      </div>
      <ol className="flex flex-col gap-1">
        {operations.map((op, i) => (
          <li
            key={i}
            className="text-xs text-ink border-l-2 border-border pl-2"
          >
            <span className="font-mono text-muted">#{i + 1}</span>{" "}
            <span className="font-mono">
              {Math.round(op.fraction * 100)}%
            </span>{" "}
            of{" "}
            <span
              className="font-mono"
              title={op.sources.join(", ")}
            >
              {formatKeyList(op.sources)}
            </span>{" "}
            → {" "}
            {op.destinations.length === 0 ? (
              <span className="text-error font-mono">dropped</span>
            ) : (
              <>
                <span
                  className="font-mono"
                  title={op.destinations.join(", ")}
                >
                  {formatKeyList(op.destinations)}
                </span>{" "}
                <span className="text-muted">({op.shape})</span>
              </>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}

/**
 * Compact display of a list of bucket keys. Short lists render
 * fully; long lists show the first two with a "+N more" tail so
 * the operations log stays scannable. The full list is available
 * via the parent element's `title` (hover tooltip) when truncated.
 *
 *   []                          → "(none)"            (shouldn't happen in practice)
 *   ["a"]                       → "a"
 *   ["a", "b"]                  → "a, b"
 *   ["a", "b", "c"]             → "a, b, c"
 *   ["a", "b", "c", "d", "e"]   → "a, b, +3 more"
 */
function formatKeyList(keys: string[]): string {
  if (keys.length === 0) return "(none)";
  if (keys.length <= 3) return keys.join(", ");
  return `${keys[0]}, ${keys[1]}, +${keys.length - 2} more`;
}

// --- Submitted-mode summary -----------------------------------------

function renderSubmittedSummary(value: RedistributionValue) {
  if (!value) return "—";
  const srcCount = Object.keys(value.mapping ?? {}).length;
  switch (value.policy) {
    case "drop":
      return `dropped (${srcCount} source bucket${srcCount === 1 ? "" : "s"})`;
    case "spread_even":
      return `spread evenly across ${srcCount} source bucket${srcCount === 1 ? "" : "s"}`;
    case "match_shape":
      return `matched destination shape (${srcCount} source bucket${srcCount === 1 ? "" : "s"})`;
    case "push_to_nearest":
      return `pushed each of ${srcCount} source bucket${srcCount === 1 ? "" : "s"} to nearest destination`;
    case "manual": {
      const opCount = value.operations?.length ?? 0;
      return (
        `manual mapping across ${srcCount} source bucket` +
        `${srcCount === 1 ? "" : "s"} ` +
        `(${opCount} operation${opCount === 1 ? "" : "s"})`
      );
    }
    default:
      return "—";
  }
}

// --- Bundle ----------------------------------------------------------

export const redistributionEditorWidget: Widget<RedistributionValue> = {
  Component: RedistributionEditorComponent,
  renderSubmitted: (value) => {
    if (!value) return "—";
    return renderSubmittedSummary(value);
  },
  validate: (value, field) => {
    if (!field.required) return null;
    if (!value) return `${field.label} is required`;
    // For deterministic policies (drop / spread_even / match_shape /
    // push_to_nearest), the mapping is fully populated by definition
    // — required is trivially satisfied.
    // For manual policy, required means "every source bucket is
    // allocated"; the in-widget counter enforces this UX. The bundle
    // doesn't have the sources list here (the StepField doesn't
    // carry it directly), so structural validation is the best we
    // can do at this layer.
    return null;
  },
};

export { operationAllocations, validateValue };
