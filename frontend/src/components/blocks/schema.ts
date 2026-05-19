/**
 * Form-schema derivation. Walks a layout tree to find the input/widget
 * blocks, then builds a Zod schema and default values for react-hook-form.
 * The form layer doesn't care where in the tree a field sits.
 */

import { z, type ZodTypeAny } from "zod";
import { type Block, type StepField } from "../../lib/api";
import {
  evalConditions,
  readConditions,
  type FieldCondition,
} from "./conditions";

/** Block types that contribute a form field. */
const FIELD_TYPES = new Set([
  "text",
  "number",
  "select",
  "textarea",
  "histogram_widget",
  "radio",
  "checkbox",
  "date",
  "email",
  "tel",
  "url",
  "time",
  "rating",
  "slider",
  "file",
  "s3file",
  "sankey",
  "date_range",
  "number_range",
  "multi_select",
  "checkbox_grid",
  "checkbox_list",
]);

export interface FieldBlock {
  id: string;
  type: string;
  label: string;
  required: boolean;
  options: string[];
  default: unknown;
  /** Visibility conditions from enclosing `When` blocks (a conjunction). */
  conditions: FieldCondition[];
  /** The raw block — needed to drive the widget bundle. */
  raw: Block;
}

/** Depth-first collection of every field block in the tree, in order. */
export function collectFields(root: Block): FieldBlock[] {
  const out: FieldBlock[] = [];
  const walk = (b: Block) => {
    if (FIELD_TYPES.has(b.type) && b.id) {
      out.push({
        id: b.id,
        type: b.type,
        label: (b.props.label as string) ?? b.id,
        required: Boolean(b.props.required),
        options: (b.props.options as string[]) ?? [],
        default: b.props.default,
        conditions: readConditions(b.props),
        raw: b,
      });
    }
    b.children.forEach(walk);
  };
  walk(root);
  return out;
}

/** All *submit* button blocks in the tree, in order. The first is the
 *  form's default submit target (activated by Enter). Link buttons —
 *  those carrying a `url` — navigate instead of submitting and are
 *  excluded. */
export function collectButtons(root: Block): { id: string; label: string }[] {
  const out: { id: string; label: string }[] = [];
  const walk = (b: Block) => {
    if (b.type === "button" && b.id && !b.props.url) {
      out.push({ id: b.id, label: (b.props.label as string) ?? "Submit" });
    }
    b.children.forEach(walk);
  };
  walk(root);
  return out;
}

/**
 * Build the StepField shape the widget bundle expects from a
 * histogram_widget block. The widget reads its data out of
 * `xcom[widget_data.xcom_key]`.
 */
export function synthWidgetField(block: Block): StepField {
  const id = block.id ?? "";
  return {
    name: id,
    label: (block.props.label as string) ?? id,
    type: "widget",
    required: Boolean(block.props.required),
    options: [],
    placeholder: "",
    default: null,
    widget: "distribution_filter",
    widget_data: {
      xcom_key: id,
      value_label: (block.props.value_label as string) ?? "value",
    },
  };
}

export function buildSchema(fields: FieldBlock[]) {
  const shape: Record<string, ZodTypeAny> = {};
  for (const f of fields) {
    // A conditional field's required-ness is enforced in superRefine
    // below — only when it's actually visible. Its base schema is kept
    // permissive so a hidden field's default value always validates.
    const isConditional = f.conditions.length > 0;
    const required = f.required && !isConditional;
    let schema: ZodTypeAny;
    switch (f.type) {
      case "number":
        schema = z
          .number({ invalid_type_error: `${f.label} is required` })
          .refine((v) => !Number.isNaN(v), {
            message: `${f.label} is required`,
          });
        schema = schema.optional();
        break;
      case "histogram_widget":
        schema = z
          .unknown()
          .refine((v) => !required || (v !== null && v !== undefined), {
            message: `${f.label} is required`,
          });
        break;
      case "checkbox":
        // A required checkbox must be ticked (the consent pattern).
        schema = required
          ? z.boolean().refine((v) => v === true, {
              message: `${f.label} is required`,
            })
          : z.boolean();
        break;
      case "multi_select":
      case "checkbox_list": {
        let s = z.array(z.string());
        if (required) s = s.min(1, `${f.label} is required`);
        schema = s;
        break;
      }
      case "date_range": {
        const base = z.object({ start: z.string(), end: z.string() });
        schema = required
          ? base.refine((v) => v.start.trim() !== "" && v.end.trim() !== "", {
              message: `${f.label} is required`,
            })
          : base;
        break;
      }
      case "number_range": {
        const part = z.union([
          z.number(),
          z.nan(),
          z.null(),
          z.undefined(),
        ]);
        const base = z.object({ min: part, max: part });
        schema = required
          ? base.refine((v) => isFiniteNumber(v.min) && isFiniteNumber(v.max), {
              message: `${f.label} is required`,
            })
          : base;
        break;
      }
      case "checkbox_grid": {
        const base = z.record(z.array(z.string()));
        schema = required
          ? base.refine(
              (v) => Object.values(v).some((cols) => cols.length > 0),
              { message: `${f.label} is required` },
            )
          : base;
        break;
      }
      case "rating":
      case "slider": {
        // Numeric single-value inputs. Rating is unset until chosen;
        // slider always carries a number.
        const part = z.union([
          z.number(),
          z.nan(),
          z.null(),
          z.undefined(),
        ]);
        schema = required
          ? part.refine((v) => isFiniteNumber(v), {
              message: `${f.label} is required`,
            })
          : part;
        break;
      }
      case "file":
      case "s3file": {
        // The value is the upload reference object, or null/undefined
        // when nothing has been uploaded.
        const base = z.union([
          z.object({}).passthrough(),
          z.null(),
          z.undefined(),
        ]);
        schema = required
          ? base.refine(
              (v) =>
                !!v &&
                typeof v === "object" &&
                (("token" in v && (v as Record<string, unknown>).token) ||
                  ("key" in v && (v as Record<string, unknown>).key)),
              { message: `${f.label} is required` },
            )
          : base;
        break;
      }
      case "sankey": {
        // A list of weighted connection triples.
        const triple = z.object({
          from: z.string(),
          to: z.string(),
          weight: z.number(),
        });
        let s = z.array(triple);
        if (required) {
          s = s.min(1, `${f.label} is required`);
        }
        // In normalize mode, each source's outgoing weights must sum
        // to 100. An empty mapping is left to the required check.
        const normalize = f.raw.props.normalize !== false;
        schema = normalize
          ? s.superRefine((links, ctx) => {
              const totals: Record<string, number> = {};
              for (const l of links) {
                totals[l.from] = (totals[l.from] ?? 0) + l.weight;
              }
              for (const [src, total] of Object.entries(totals)) {
                if (total !== 100) {
                  ctx.addIssue({
                    code: z.ZodIssueCode.custom,
                    message:
                      `${f.label}: weights from "${src}" total ` +
                      `${total}, must total 100`,
                  });
                }
              }
            })
          : s;
        break;
      }
      default: {
        // text, select, textarea, radio, date, email, tel, url, time —
        // all string-valued.
        let s = z.string();
        if (required) s = s.min(1, `${f.label} is required`);
        schema = required ? s : s.optional();
        break;
      }
    }
    shape[f.id] = schema;
  }

  // Conditional fields that are required: enforce required-ness only
  // when the field's `When` conditions are currently satisfied.
  const conditionalRequired = fields.filter(
    (f) => f.required && f.conditions.length > 0,
  );
  return z.object(shape).superRefine((vals, ctx) => {
    const values = vals as Record<string, unknown>;
    for (const f of conditionalRequired) {
      if (!evalConditions(f.conditions, values)) continue;
      if (isBlank(f.type, values[f.id])) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: [f.id],
          message: `${f.label} is required`,
        });
      }
    }
  });
}

/** A real, finite number (excludes NaN / null / undefined). */
function isFiniteNumber(x: unknown): x is number {
  return typeof x === "number" && !Number.isNaN(x);
}

/**
 * Whether a submitted value counts as empty for a given field type.
 * Mirrors the backend `_value_is_blank` — used to enforce required-ness
 * for conditionally-visible fields.
 */
export function isBlank(type: string, value: unknown): boolean {
  if (value === null || value === undefined) return true;
  switch (type) {
    case "checkbox":
      return value !== true;
    case "multi_select":
    case "checkbox_list":
      return !Array.isArray(value) || value.length === 0;
    case "checkbox_grid":
      return (
        typeof value !== "object" ||
        !Object.values(value as Record<string, unknown>).some(
          (c) => Array.isArray(c) && c.length > 0,
        )
      );
    case "date_range": {
      const v = value as { start?: string; end?: string };
      return (
        typeof value !== "object" ||
        !v.start?.trim() ||
        !v.end?.trim()
      );
    }
    case "number_range": {
      const v = value as { min?: unknown; max?: unknown };
      return (
        typeof value !== "object" ||
        !isFiniteNumber(v.min) ||
        !isFiniteNumber(v.max)
      );
    }
    case "number":
    case "rating":
    case "slider":
      return !isFiniteNumber(value);
    case "file":
    case "s3file": {
      if (typeof value !== "object" || value === null) return true;
      const v = value as Record<string, unknown>;
      return !(v.token || v.key);
    }
    case "sankey":
      return !Array.isArray(value) || value.length === 0;
    case "histogram_widget":
      return value === null || value === undefined;
    default:
      return String(value).trim() === "";
  }
}

export function buildDefaults(fields: FieldBlock[]): Record<string, unknown> {
  const d: Record<string, unknown> = {};
  for (const f of fields) {
    if (f.default !== null && f.default !== undefined) {
      d[f.id] = f.default;
      continue;
    }
    switch (f.type) {
      case "number":
      case "histogram_widget":
        d[f.id] = undefined;
        break;
      case "checkbox":
        d[f.id] = false;
        break;
      case "multi_select":
      case "checkbox_list":
        d[f.id] = [];
        break;
      case "sankey":
        // A list of connection triples — empty until links are drawn.
        d[f.id] = [];
        break;
      case "file":
      case "s3file":
        // No upload yet — the upload reference is null until one is
        // made. (Not "" — the field components expect an object|null.)
        d[f.id] = null;
        break;
      case "date_range":
        d[f.id] = { start: "", end: "" };
        break;
      case "number_range":
        d[f.id] = { min: undefined, max: undefined };
        break;
      case "checkbox_grid":
        d[f.id] = {};
        break;
      default:
        // text, select, textarea, radio, date
        d[f.id] = "";
        break;
    }
  }
  return d;
}
