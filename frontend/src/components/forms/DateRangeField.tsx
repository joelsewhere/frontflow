import { Field } from "./Field";

export interface DateRangeValue {
  start: string;
  end: string;
}

interface DateRangeFieldProps {
  label: string;
  value: DateRangeValue | undefined;
  onChange: (v: DateRangeValue) => void;
  error?: string;
  hint?: string;
}

const INPUT_CLASS =
  "bg-surface border rounded-theme px-3 py-3 text-base text-ink " +
  "font-sans outline-none transition-colors focus:border-ink";

/**
 * A start/end pair of native date pickers. Controlled — drive it with
 * react-hook-form's <Controller>. Value: `{start, end}` (ISO strings).
 * Each picker bounds the other so the range can't invert.
 */
export function DateRangeField({
  label,
  value,
  onChange,
  error, hint,
}: DateRangeFieldProps) {
  const v = value ?? { start: "", end: "" };
  const border = error ? "border-error" : "border-border";
  return (
    <Field label={label} error={error} hint={hint}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <input
          type="date"
          value={v.start}
          max={v.end || undefined}
          onChange={(e) => onChange({ ...v, start: e.target.value })}
          className={`${INPUT_CLASS} ${border} flex-1`}
          aria-label={`${label} — start`}
        />
        <span className="font-mono text-sm text-muted">→</span>
        <input
          type="date"
          value={v.end}
          min={v.start || undefined}
          onChange={(e) => onChange({ ...v, end: e.target.value })}
          className={`${INPUT_CLASS} ${border} flex-1`}
          aria-label={`${label} — end`}
        />
      </div>
    </Field>
  );
}
