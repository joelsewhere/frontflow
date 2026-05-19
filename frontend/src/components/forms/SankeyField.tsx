import { useMemo, useState } from "react";
import { Field } from "./Field";

/** One weighted connection: a source-column value to a target-column
 *  value, carrying a weight. */
export interface SankeyLink {
  from: string;
  to: string;
  weight: number;
}

interface SankeyFieldProps {
  label: string;
  fieldId: string;
  columnA: string[];
  columnB: string[];
  /** When true, each source's outgoing weights must sum to 100. */
  normalize: boolean;
  error?: string;
  hint?: string;
  value: SankeyLink[];
  onChange: (value: SankeyLink[]) => void;
  /** Read-only render (submitted/review mode). */
  readOnly?: boolean;
}

// Layout constants for the SVG diagram.
const NODE_H = 34;
const NODE_GAP = 14;
const COL_W = 150;
const RIBBON_W = 220; // horizontal span between the columns
const PAD = 12;

/**
 * Interactive weighted Sankey mapping. The chart is drawn directly as
 * SVG — two columns of nodes with bezier ribbons whose thickness
 * tracks weight. The interaction is bespoke: pick a source, then a
 * target, to create a link; each link has an editable weight; click a
 * link's row to remove it. In `normalize` mode each source shows its
 * running total against 100.
 */
export function SankeyField({
  label,
  fieldId,
  columnA,
  columnB,
  normalize,
  error,
  hint,
  value,
  onChange,
  readOnly = false,
}: SankeyFieldProps) {
  const links = Array.isArray(value) ? value : [];
  const [pendingFrom, setPendingFrom] = useState<string | null>(null);

  // Per-source outgoing total — for the normalize indicator.
  const sourceTotals = useMemo(() => {
    const t: Record<string, number> = {};
    for (const a of columnA) t[a] = 0;
    for (const l of links) t[l.from] = (t[l.from] ?? 0) + l.weight;
    return t;
  }, [links, columnA]);

  const rows = Math.max(columnA.length, columnB.length, 1);
  const height = rows * NODE_H + (rows - 1) * NODE_GAP + PAD * 2;
  const width = COL_W * 2 + RIBBON_W + PAD * 2;

  function nodeY(index: number, count: number): number {
    // Vertically centre each column's nodes in the SVG height.
    const colH = count * NODE_H + (count - 1) * NODE_GAP;
    const top = (height - colH) / 2;
    return top + index * (NODE_H + NODE_GAP);
  }

  function addLink(from: string, to: string) {
    // Default weight: in normalize mode, whatever is left to reach 100
    // (so the common case needs no typing); otherwise 1.
    const existing = links.filter((l) => l.from === from);
    let weight = normalize
      ? Math.max(
          0,
          100 - existing.reduce((s, l) => s + l.weight, 0),
        )
      : 1;
    if (weight === 0) weight = normalize ? 0 : 1;
    onChange([...links, { from, to, weight }]);
  }

  function handleTargetClick(to: string) {
    if (readOnly || pendingFrom === null) return;
    const dup = links.some(
      (l) => l.from === pendingFrom && l.to === to,
    );
    if (!dup) addLink(pendingFrom, to);
    setPendingFrom(null);
  }

  function removeLink(i: number) {
    if (readOnly) return;
    onChange(links.filter((_, idx) => idx !== i));
  }

  function setWeight(i: number, weight: number) {
    if (readOnly) return;
    onChange(
      links.map((l, idx) => (idx === i ? { ...l, weight } : l)),
    );
  }

  const maxWeight = Math.max(1, ...links.map((l) => l.weight));

  return (
    <Field label={label} htmlFor={fieldId} error={error} hint={hint}>
      <div className="flex flex-col gap-3">
        {/* The diagram */}
        <svg
          viewBox={`0 0 ${width} ${height}`}
          className="w-full select-none"
          style={{ maxHeight: height }}
        >
          {/* Ribbons */}
          {links.map((l, i) => {
            const ai = columnA.indexOf(l.from);
            const bi = columnB.indexOf(l.to);
            if (ai < 0 || bi < 0) return null;
            const y1 = nodeY(ai, columnA.length) + NODE_H / 2;
            const y2 = nodeY(bi, columnB.length) + NODE_H / 2;
            const x1 = PAD + COL_W;
            const x2 = PAD + COL_W + RIBBON_W;
            const mx = (x1 + x2) / 2;
            const thickness = 2 + (l.weight / maxWeight) * 16;
            return (
              <path
                key={i}
                d={`M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`}
                fill="none"
                stroke="rgb(var(--color-accent))"
                strokeWidth={thickness}
                strokeOpacity={0.4}
                strokeLinecap="round"
              />
            );
          })}
          {/* Column A nodes (sources) */}
          {columnA.map((a, i) => {
            const y = nodeY(i, columnA.length);
            const selected = pendingFrom === a;
            const total = sourceTotals[a] ?? 0;
            const offTarget =
              normalize && total !== 0 && total !== 100;
            return (
              <g
                key={a}
                onClick={() =>
                  !readOnly && setPendingFrom(selected ? null : a)
                }
                style={{
                  cursor: readOnly ? "default" : "pointer",
                  pointerEvents: readOnly ? "none" : "all",
                }}
              >
                <rect
                  x={PAD}
                  y={y}
                  width={COL_W}
                  height={NODE_H}
                  rx={4}
                  fill={
                    selected
                      ? "rgb(var(--color-accent))"
                      : "rgb(var(--color-surface))"
                  }
                  stroke={
                    offTarget
                      ? "rgb(var(--color-error))"
                      : "rgb(var(--color-border))"
                  }
                />
                <text
                  pointerEvents="none"
                  x={PAD + 10}
                  y={y + NODE_H / 2 + 4}
                  fontSize="12"
                  fill={
                    selected
                      ? "rgb(var(--color-bg))"
                      : "rgb(var(--color-ink))"
                  }
                >
                  {a}
                </text>
                {normalize && total > 0 && (
                  <text
                  pointerEvents="none"
                    x={PAD + COL_W - 10}
                    y={y + NODE_H / 2 + 4}
                    fontSize="10"
                    textAnchor="end"
                    fill={
                      offTarget
                        ? "rgb(var(--color-error))"
                        : "rgb(var(--color-muted))"
                    }
                  >
                    {total}%
                  </text>
                )}
              </g>
            );
          })}
          {/* Column B nodes (targets) */}
          {columnB.map((b, i) => {
            const y = nodeY(i, columnB.length);
            return (
              <g
                key={b}
                onClick={() => handleTargetClick(b)}
                style={{
                  cursor:
                    !readOnly && pendingFrom !== null
                      ? "pointer"
                      : "default",
                  pointerEvents: readOnly ? "none" : "all",
                }}
              >
                <rect
                  x={PAD + COL_W + RIBBON_W}
                  y={y}
                  width={COL_W}
                  height={NODE_H}
                  rx={4}
                  fill="rgb(var(--color-surface))"
                  stroke="rgb(var(--color-border))"
                />
                <text
                  pointerEvents="none"
                  x={PAD + COL_W + RIBBON_W + 10}
                  y={y + NODE_H / 2 + 4}
                  fontSize="12"
                  fill="rgb(var(--color-ink))"
                >
                  {b}
                </text>
              </g>
            );
          })}
        </svg>

        {/* Interaction hint */}
        {!readOnly && (
          <p className="font-mono text-[11px] text-muted">
            {pendingFrom
              ? `Pick a target for "${pendingFrom}".`
              : "Click a source, then a target, to connect them."}
          </p>
        )}

        {/* Link list with weights */}
        {links.length > 0 && (
          <ul className="flex flex-col gap-1">
            {links.map((l, i) => {
              const offTarget =
                normalize &&
                sourceTotals[l.from] !== 100 &&
                sourceTotals[l.from] !== 0;
              return (
                <li
                  key={i}
                  className="flex items-center gap-2 text-sm text-ink"
                >
                  <span className="min-w-0 flex-1 truncate font-sans">
                    {l.from} → {l.to}
                  </span>
                  {readOnly ? (
                    <span className="font-mono text-sm">
                      {l.weight}
                      {normalize ? "%" : ""}
                    </span>
                  ) : (
                    <>
                      <input
                        type="number"
                        value={l.weight}
                        min={0}
                        onChange={(e) =>
                          setWeight(i, Number(e.target.value) || 0)
                        }
                        className={[
                          "w-20 border bg-surface px-2 py-1 text-right",
                          "font-mono text-sm text-ink focus:outline-none",
                          offTarget
                            ? "border-error"
                            : "border-border focus:border-ink",
                        ].join(" ")}
                      />
                      {normalize && (
                        <span className="font-mono text-xs text-muted">
                          %
                        </span>
                      )}
                      <button
                        type="button"
                        onClick={() => removeLink(i)}
                        className="font-mono text-[10px] uppercase tracking-[0.16em] text-muted hover:text-error"
                      >
                        Remove
                      </button>
                    </>
                  )}
                </li>
              );
            })}
          </ul>
        )}

        {normalize && !readOnly && (
          <p className="font-mono text-[11px] text-muted">
            Each source's weights should total 100%. Sources off 100
            are outlined in red.
          </p>
        )}
      </div>
    </Field>
  );
}
