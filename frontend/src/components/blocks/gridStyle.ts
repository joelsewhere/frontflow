/**
 * Turning a Grid block's props into style — the parts worth testing
 * without a browser.
 *
 * Two traps this exists to avoid, both of which have cost real time in
 * this project:
 *
 * 1. **Tailwind cannot generate a class name from a runtime value.**
 *    `grid-cols-${n}` scans as nothing, emits nothing, and silently
 *    produces a one-column grid. Every class below is a literal string
 *    in a lookup, so the scanner sees them all.
 *
 * 2. **A media query cannot live in an inline style.** The column count
 *    is passed as a CSS custom property and consumed by a real rule in
 *    index.css, which is what lets the grid collapse to one column on a
 *    narrow screen.
 */

export const GRID_MAX_COLUMNS = 12;

/** align -> a literal Tailwind class. Unknown values fall back to stretch. */
const ALIGN_CLASS: Record<string, string> = {
  start: "items-start",
  center: "items-center",
  end: "items-end",
  stretch: "items-stretch",
};

export function alignClassFor(align: unknown): string {
  if (typeof align !== "string") return ALIGN_CLASS.stretch;
  return ALIGN_CLASS[align] ?? ALIGN_CLASS.stretch;
}

/**
 * Clamp to something renderable. The DSL validates this already, but a
 * compiled graph can be older than the validator that would have
 * rejected it, so the frontend does not get to assume.
 */
export function columnsFor(columns: unknown): number {
  const n =
    typeof columns === "number" && Number.isFinite(columns)
      ? Math.floor(columns)
      : 1;
  if (n < 1) return 1;
  if (n > GRID_MAX_COLUMNS) return GRID_MAX_COLUMNS;
  return n;
}

/**
 * The custom property index.css reads. Returned as a style object rather
 * than a class so the value can be any number without Tailwind needing
 * to have seen it.
 */
export function gridStyleFor(columns: unknown): Record<string, string> {
  return { "--ff-grid-cols": String(columnsFor(columns)) };
}

/**
 * A cell's span. `span 1` is the default and is emitted as no style at
 * all, so an ordinary child carries no inline attribute.
 */
export function cellStyleFor(span: unknown): Record<string, string> | undefined {
  const n = columnsFor(span);
  if (n <= 1) return undefined;
  return { gridColumn: `span ${n} / span ${n}` };
}


/**
 * The style for one grid ITEM, given the child it wraps.
 *
 * GridBlock wraps every child in a div to give it `min-w-0`, and that
 * wrapper — not the child — is the grid item. A span set on the child
 * therefore applies to a plain block inside a grid cell and does
 * nothing at all: the Cell occupies one column and the columns it
 * should have covered sit empty.
 *
 * So the span has to be lifted onto the wrapper, which is what this
 * does. It reads the child rather than being told, so GridBlock does
 * not have to know what a cell is.
 */
export function gridItemStyleFor(child: {
  type?: unknown;
  props?: { span?: unknown } | null;
}): Record<string, string> | undefined {
  if (!child || child.type !== "cell") return undefined;
  return cellStyleFor(child.props?.span);
}
