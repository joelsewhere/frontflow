import { forwardRef, type SelectHTMLAttributes } from "react";
import { Field } from "./Field";

interface SelectFieldProps extends SelectHTMLAttributes<HTMLSelectElement> {
  label: string;
  options: readonly string[];
  error?: string;
  hint?: string;
  placeholder?: string;
}

/**
 * Native select primitive. Same pattern as TextField — spread `register()`.
 * If `placeholder` is provided, renders a disabled-by-default empty option
 * at the top so the user must explicitly choose.
 */
export const SelectField = forwardRef<HTMLSelectElement, SelectFieldProps>(
  function SelectField(
    { label, options, error, hint, id, name, placeholder, ...rest },
    ref,
  ) {
    const inputId = id ?? name;
    return (
      <Field label={label} htmlFor={inputId} error={error} hint={hint}>
        <div className="relative">
          <select
            ref={ref}
            id={inputId}
            name={name}
            {...rest}
            className={[
              "w-full bg-surface border border-border rounded-theme appearance-none",
              "px-4 py-3 pr-10 text-base text-ink",
              "font-sans",
              "outline-none transition-colors cursor-pointer",
              "focus:border-ink",
              error ? "border-error" : "",
              rest.className ?? "",
            ].join(" ")}
            defaultValue={rest.defaultValue ?? (placeholder ? "" : undefined)}
          >
            {placeholder ? (
              <option value="" disabled>
                {placeholder}
              </option>
            ) : null}
            {options.map((opt) => (
              <option key={opt} value={opt}>
                {opt}
              </option>
            ))}
          </select>
          {/* Custom caret since we removed the native appearance */}
          <span
            aria-hidden
            className="pointer-events-none absolute right-4 top-1/2 -translate-y-1/2 text-muted"
          >
            ▾
          </span>
        </div>
      </Field>
    );
  },
);
