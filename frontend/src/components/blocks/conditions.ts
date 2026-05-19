/**
 * Conditional layout — the frontend half. Mirrors backend
 * conditions.py: evaluates field conditions against form values to
 * decide whether a `When` block (and the fields inside it) is visible.
 */

export interface FieldCondition {
  field: string;
  op: "equals" | "not_equals" | "in" | "not_in" | "contains" | "truthy" | "falsy";
  value: unknown;
}

/** Evaluate one condition against the current form values. */
function evalCondition(
  cond: FieldCondition,
  values: Record<string, unknown>,
): boolean {
  const actual = values[cond.field];
  switch (cond.op) {
    case "equals":
      return actual === cond.value;
    case "not_equals":
      return actual !== cond.value;
    case "in":
      return Array.isArray(cond.value) && cond.value.includes(actual);
    case "not_in":
      return Array.isArray(cond.value) && !cond.value.includes(actual);
    case "contains": {
      // `actual` is expected to be a list (multi-select, HITL
      // chosen_options); tolerate a scalar as a one-element list.
      const items = Array.isArray(actual) ? actual : [actual];
      return items.includes(cond.value);
    }
    case "truthy":
      return isTruthy(actual);
    case "falsy":
      return !isTruthy(actual);
    default:
      return true;
  }
}

/**
 * Whether a value counts as "filled" for a truthy/falsy condition.
 * Empty strings, empty arrays, and empty objects are not filled — so
 * `is_filled()` behaves intuitively across every input type.
 */
function isTruthy(value: unknown): boolean {
  if (value === null || value === undefined) return false;
  if (typeof value === "string") return value.trim() !== "";
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return !Number.isNaN(value);
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") {
    return Object.values(value as Record<string, unknown>).some(isTruthy);
  }
  return Boolean(value);
}

/** A field/`When` is active when every one of its conditions holds. */
export function evalConditions(
  conditions: FieldCondition[] | undefined,
  values: Record<string, unknown>,
): boolean {
  if (!conditions || conditions.length === 0) return true;
  return conditions.every((c) => evalCondition(c, values));
}

/** Read a block's `conditions` prop into a typed array. */
export function readConditions(
  props: Record<string, unknown>,
): FieldCondition[] {
  const raw = props.conditions;
  return Array.isArray(raw) ? (raw as FieldCondition[]) : [];
}

/** A template token: `{{ steps.<node>.<field> }}` — the one namespace
 *  shared with submission_id templating and branch conditions. */
const TEMPLATE_RE =
  /\{\{\s*steps\s*\.\s*([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\s*\}\}/g;

/**
 * Live templating — substitute `{{ steps.<node>.<field> }}` tokens
 * with a field's current value. A token naming the node being
 * rendered (`currentNodeId`) resolves against `values`, updating as
 * the user types; a token naming any other node is left untouched
 * (the server resolves those to literals before the layout is sent).
 * With `encode`, substituted values are percent-encoded — for use
 * inside a URL.
 */
export function interpolate(
  text: string,
  values: Record<string, unknown>,
  currentNodeId: string,
  opts?: { encode?: boolean },
): string {
  if (!text.includes("{{")) return text;
  return text.replace(TEMPLATE_RE, (whole, node: string, field: string) => {
    if (node !== currentNodeId) return whole;
    const v = values[field];
    const s = v === null || v === undefined ? "" : String(v);
    return opts?.encode ? encodeURIComponent(s) : s;
  });
}
