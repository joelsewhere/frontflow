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
        "inline-flex items-center gap-1.5 border border-border bg-bg",
        "px-2.5 py-1 text-xs font-mono cursor-grab active:cursor-grabbing",
        "select-none",
      ].join(" ")}
    >
      <span className="truncate max-w-[16rem]">{item}</span>
      {onRemove ? (
        <button
          type="button"
          onClick={onRemove}
          aria-label={`Unassign ${item}`}
          className="text-muted hover:text-error leading-none"
        >
          ×
        </button>
      ) : null}
    </div>
  );
}

function DropZone({
  title,
  count,
  onDropItem,
  children,
  grow,
}: {
  title: string;
  count: number;
  onDropItem: (item: string, from: string) => void;
  children: React.ReactNode;
  grow?: boolean;
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
    <div
      onDragOver={(e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={handleDrop}
      className={[
        "flex flex-col gap-2 border p-3 min-h-[7rem] transition-colors",
        over ? "border-accent bg-accent/5" : "border-border bg-surface",
        grow ? "flex-1 min-w-0" : "",
      ].join(" ")}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-[10px] uppercase tracking-[0.2em] text-muted font-mono">
          {title}
        </span>
        <span className="text-[10px] text-muted font-mono">{count}</span>
      </div>
      <div className="flex flex-wrap gap-1.5 content-start">{children}</div>
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
        <div className="flex flex-col sm:flex-row gap-3">
          {categories.map((cat) => (
            <DropZone
              key={cat.id}
              title={cat.label}
              count={(current[cat.id] ?? []).length}
              onDropItem={(item, from) => move(item, from, cat.id)}
              grow
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
            <div key={c.id} className="text-sm">
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
