import { Field } from "./Field";

interface CheckboxListFieldProps {
  label: string;
  options: readonly string[];
  /** Fixed grid column count; omit for a responsive layout. */
  columns?: number;
  value: string[] | undefined;
  onChange: (v: string[]) => void;
  error?: string;
  hint?: string;
}

/**
 * A flat list of options, each with its own checkbox, laid out as a
 * grid — every option visible at once (unlike the search-as-you-type
 * MultiSelect). Controlled — drive it with react-hook-form's
 * <Controller>. Value is the list of checked option strings.
 */
export function CheckboxListField({
  label,
  options,
  columns,
  value,
  onChange,
  error, hint,
}: CheckboxListFieldProps) {
  const selected = value ?? [];

  const toggle = (option: string) => {
    onChange(
      selected.includes(option)
        ? selected.filter((o) => o !== option)
        : [...selected, option],
    );
  };

  // A fixed column count when given, else a responsive 1/2-column grid.
  const gridStyle = columns
    ? { gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }
    : undefined;
  const gridClass = columns
    ? "grid gap-x-6 gap-y-2.5"
    : "grid gap-x-6 gap-y-2.5 grid-cols-1 sm:grid-cols-2";

  return (
    <Field label={label} error={error} hint={hint}>
      <div
        className={`border px-4 py-3 ${
          error ? "border-error" : "border-border"
        }`}
      >
        <div className={gridClass} style={gridStyle}>
          {options.map((option) => (
            <label
              key={option}
              className="inline-flex items-center gap-3 cursor-pointer select-none"
            >
              <input
                type="checkbox"
                checked={selected.includes(option)}
                onChange={() => toggle(option)}
                className="h-4 w-4 cursor-pointer accent-ink shrink-0"
              />
              <span className="text-sm text-ink font-sans">{option}</span>
            </label>
          ))}
        </div>
      </div>
    </Field>
  );
}
