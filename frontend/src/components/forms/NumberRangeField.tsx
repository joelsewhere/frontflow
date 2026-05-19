import { Field } from "./Field";

export interface NumberRangeValue {
  min: number | undefined;
  max: number | undefined;
}

interface NumberRangeFieldProps {
  label: string;
  value: NumberRangeValue | undefined;
  onChange: (v: NumberRangeValue) => void;
  error?: string;
  hint?: string;
}

const INPUT_CLASS =
  "bg-surface border rounded-theme px-3 py-3 text-base text-ink " +
  "placeholder:text-muted/60 font-sans outline-none transition-colors " +
  "focus:border-ink";

/**
 * A low/high pair of numeric inputs. Controlled — drive it with
 * react-hook-form's <Controller>. Value: `{min, max}`; an empty input
 * reads as `undefined` rather than NaN.
 */
export function NumberRangeField({
  label,
  value,
  onChange,
  error, hint,
}: NumberRangeFieldProps) {
  const v = value ?? { min: undefined, max: undefined };
  const border = error ? "border-error" : "border-border";
  const parse = (raw: string): number | undefined =>
    raw === "" ? undefined : Number(raw);
  return (
    <Field label={label} error={error} hint={hint}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <input
          type="number"
          inputMode="decimal"
          placeholder="Min"
          value={v.min ?? ""}
          onChange={(e) => onChange({ ...v, min: parse(e.target.value) })}
          className={`${INPUT_CLASS} ${border} flex-1`}
          aria-label={`${label} — minimum`}
        />
        <span className="font-mono text-sm text-muted">–</span>
        <input
          type="number"
          inputMode="decimal"
          placeholder="Max"
          value={v.max ?? ""}
          onChange={(e) => onChange({ ...v, max: parse(e.target.value) })}
          className={`${INPUT_CLASS} ${border} flex-1`}
          aria-label={`${label} — maximum`}
        />
      </div>
    </Field>
  );
}
