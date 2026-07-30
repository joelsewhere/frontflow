import { useState, type DragEvent } from "react";
import { BaseWidget } from "./BaseWidget";
import { type Widget, type WidgetProps } from "./types";

/**
 * Categorizer widget: a bank of item chips plus one column per
 * category. The user drags a chip from the bank into a column (or
 * between columns, or back to the bank). An item lives in at most one
 * place, so overlapping classifications can't be expressed at all.
 *
 * Value shape: { [categoryId]: string[] } — every category id present,
 * empty array when nothing was dropped there. Items still in the bank
 * are simply absent from the value.
 *
 * Native HTML5 drag & drop — no library. Chips also carry a compact
 * "×" (in columns) for quick send-back-to-bank without dragging.
 */
export type CategorizerValue = Record<string, string[]>;

interface Category {
  id: string;
  label: string;
  /** Optional CSS color tinting the column's heading, top border,
   * and drop highlight. */
  color?: string;
}

function seedValue(categories: Category[]): CategorizerValue {
  return Object.fromEntries(categories.map((c) => [c.id, []]));
}

function Chip({
  item,
  from,
  onRemove,
}: {
  item: string;
  from: string; // "" = bank, else category id
  onRemove?: () => void;
}) {
  return (
    <div
      draggable
      onDragStart={(e) => {
        e.dataTransfer.setData("text/plain", JSON.stringify({ item, from }));
        e.dataTransfer.effectAllowed = "move";
      }}
      className={[
        "inline-flex items-start gap-1.5 border border-border bg-bg",
        "px-2.5 py-1 text-xs font-mono cursor-grab active:cursor-grabbing",
        "select-none min-w-0 max-w-full",
      ].join(" ")}
    >
      <span className="whitespace-normal break-words text-left min-w-0">
        {item}
      </span>
      {onRemove ? (
        <button
          type="button"
          onClick={onRemove}
          aria-label={`Unassign ${item}`}
          className="text-muted hover:text-error leading-none shrink-0"
        >
          ×
        </button>
      ) : null}
    </div>
  );
}

/** color-mix tint that works with any CSS color the author passes. */
function tint(color: string, pct: number): string {
  return `color-mix(in srgb, ${color} ${pct}%, transparent)`;
}

function DropZone({
  title,
  count,
  color,
  onDropItem,
  children,
}: {
  title: string;
  count: number;
  /** Optional accent — heading text, top border, drop highlight. */
  color?: string;
  onDropItem: (item: string, from: string) => void;
  children: React.ReactNode;
}) {
  const [over, setOver] = useState(false);
  const handleDrop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    try {
      const { item, from } = JSON.parse(
        e.dataTransfer.getData("text/plain"),
      ) as { item: string; from: string };
      if (item) onDropItem(item, from);
    } catch {
      /* foreign drag payload — ignore */
    }
  };
  return (
    <div className="flex flex-col gap-1.5 min-w-0">
      {/* Title sits ABOVE the drop box, in the column's accent color. */}
      <div className="flex items-baseline justify-between gap-2 px-0.5">
        <span
          className="text-[10px] uppercase tracking-[0.2em] font-mono text-muted"
          style={color ? { color } : undefined}
        >
          {title}
        </span>
        <span className="text-[10px] text-muted font-mono">{count}</span>
      </div>
      <div
        onDragOver={(e) => {
          e.preventDefault();
          e.dataTransfer.dropEffect = "move";
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={handleDrop}
        className={[
          "flex flex-wrap gap-1.5 content-start border p-3",
          "min-h-[7rem] flex-1 transition-colors",
          over && !color ? "border-accent bg-accent/5" : "",
          !over ? "border-border bg-surface" : "",
        ].join(" ")}
        style={{
          ...(color ? { borderTop: `3px solid ${color}` } : {}),
          ...(over && color
            ? { borderColor: color, background: tint(color, 8) }
            : {}),
        }}
      >
        {children}
      </div>
    </div>
  );
}

function CategorizerComponent({
  field,
  xcom,
  value,
  onChange,
  error,
}: WidgetProps<CategorizerValue>) {
  const xcomKey = (field.widget_data?.xcom_key as string | undefined) ?? "";
  const options = (xcom[xcomKey] as string[] | undefined) ?? [];
  const categories =
    (field.widget_data?.categories as Category[] | undefined) ?? [];
  const bankLabel =
    (field.widget_data?.bank_label as string | undefined) ?? "Unassigned";

  const current: CategorizerValue = value ?? seedValue(categories);
  const assigned = new Set(Object.values(current).flat());
  const bank = options.filter((o) => !assigned.has(o));

  const move = (item: string, from: string, to: string) => {
    if (from === to) return;
    const next: CategorizerValue = Object.fromEntries(
      categories.map((c) => {
        const items = (current[c.id] ?? []).filter((i) => i !== item);
        if (c.id === to) items.push(item);
        return [c.id, items];
      }),
    );
    onChange(next);
  };

  return (
    <BaseWidget
      label={field.label}
      error={error}
      hint="Drag charge codes from the bank into a column. Drop back on the bank (or click ×) to unassign."
    >
      <div className="flex flex-col gap-3">
        <DropZone
          title={bankLabel}
          count={bank.length}
          onDropItem={(item, from) => move(item, from, "")}
        >
          {bank.map((item) => (
            <Chip key={item} item={item} from="" />
          ))}
          {bank.length === 0 ? (
            <span className="text-xs text-muted">
              Everything is classified.
            </span>
          ) : null}
        </DropZone>
        {/* One row, one equal-width column per category — chip text
            wraps inside a column rather than the columns wrapping
            into rows. Width comes from the form's themable node
            width. */}
        <div
          className="grid gap-3"
          style={{
            gridTemplateColumns: `repeat(${categories.length}, minmax(0, 1fr))`,
          }}
        >
          {categories.map((cat) => (
            <DropZone
              key={cat.id}
              title={cat.label}
              count={(current[cat.id] ?? []).length}
              color={cat.color}
              onDropItem={(item, from) => move(item, from, cat.id)}
            >
              {(current[cat.id] ?? []).map((item) => (
                <Chip
                  key={item}
                  item={item}
                  from={cat.id}
                  onRemove={() => move(item, cat.id, "")}
                />
              ))}
            </DropZone>
          ))}
        </div>
      </div>
    </BaseWidget>
  );
}

export const categorizerWidget: Widget<CategorizerValue> = {
  Component: CategorizerComponent,
  renderSubmitted: (value, field) => {
    const categories =
      (field.widget_data?.categories as Category[] | undefined) ?? [];
    return (
      <div className="flex flex-col gap-1">
        {categories.map((c) => {
          const items = value?.[c.id] ?? [];
          if (items.length === 0) return null;
          return (
            <div key={c.id} className="text-sm flex items-baseline gap-1.5">
              {c.color ? (
                <span
                  aria-hidden
                  className="inline-block w-2 h-2 rounded-full shrink-0 self-center"
                  style={{ background: c.color }}
                />
              ) : null}
              <span className="text-muted">{c.label}: </span>
              <span className="font-mono text-xs">{items.join(", ")}</span>
            </div>
          );
        })}
      </div>
    );
  },
  validate: (value, field) => {
    if (!field.required) return null;
    const assigned = Object.values(value ?? {}).flat();
    return assigned.length > 0
      ? null
      : "Drag at least one item into a category.";
  },
};
