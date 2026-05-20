interface TabsProps<T extends string> {
  tabs: Array<{ id: T; label: string }>;
  active: T;
  onChange: (id: T) => void;
}

/**
 * Horizontal tab strip — small-caps mono labels, underline + ink color
 * on the active tab, muted on the rest. Renders as a `role="tablist"`
 * row sitting above the swappable content the caller renders. Wraps on
 * narrow screens.
 */
export function Tabs<T extends string>({
  tabs,
  active,
  onChange,
}: TabsProps<T>) {
  return (
    <div
      role="tablist"
      className="flex flex-wrap gap-x-6 gap-y-2 border-b border-border"
    >
      {tabs.map((t) => {
        const selected = t.id === active;
        return (
          <button
            key={t.id}
            role="tab"
            type="button"
            aria-selected={selected}
            onClick={() => onChange(t.id)}
            className={
              "relative -mb-px border-b-2 py-3 font-mono text-xs uppercase tracking-[0.18em] transition-colors " +
              (selected
                ? "border-ink text-ink"
                : "border-transparent text-muted hover:text-ink")
            }
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}
