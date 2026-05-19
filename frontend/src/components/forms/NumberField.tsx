import { forwardRef, type InputHTMLAttributes } from "react";
import { Field } from "./Field";

interface NumberFieldProps extends InputHTMLAttributes<HTMLInputElement> {
  label: string;
  error?: string;
  hint?: string;
}

/**
 * Numeric input primitive. Same API as TextField — spread `register()`
 * (with `valueAsNumber: true` if you want numbers, not strings, in your
 * form state).
 */
export const NumberField = forwardRef<HTMLInputElement, NumberFieldProps>(
  function NumberField({ label, error, hint, id, name, ...rest }, ref) {
    const inputId = id ?? name;
    return (
      <Field label={label} htmlFor={inputId} error={error} hint={hint}>
        <input
          ref={ref}
          id={inputId}
          name={name}
          type="number"
          inputMode="decimal"
          {...rest}
          className={[
            "w-full bg-surface border border-border rounded-theme",
            "px-4 py-3 text-base text-ink placeholder:text-muted/60",
            "font-sans",
            "outline-none transition-colors",
            "focus:border-ink",
            error ? "border-error" : "",
            rest.className ?? "",
          ].join(" ")}
        />
      </Field>
    );
  },
);
