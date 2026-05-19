import { forwardRef, type InputHTMLAttributes } from "react";

interface CheckboxFieldProps extends InputHTMLAttributes<HTMLInputElement> {
  label: string;
  error?: string;
  hint?: string;
}

/**
 * Checkbox primitive. Layout is inline (checkbox + label on the same row)
 * which differs from the Field shell used by the other primitives, since
 * checkboxes read better that way.
 */
export const CheckboxField = forwardRef<HTMLInputElement, CheckboxFieldProps>(
  function CheckboxField({ label, error, hint, id, name, ...rest }, ref) {
    const inputId = id ?? name;
    return (
      <div className="flex flex-col gap-2">
        <label
          htmlFor={inputId}
          className="inline-flex items-center gap-3 cursor-pointer select-none"
        >
          <input
            ref={ref}
            id={inputId}
            name={name}
            type="checkbox"
            {...rest}
            className={[
              "w-4 h-4 accent-ink cursor-pointer",
              rest.className ?? "",
            ].join(" ")}
          />
          <span className="text-base text-ink font-sans">{label}</span>
        </label>
        {error ? <p className="text-sm text-error">{error}</p> : null}
      </div>
    );
  },
);
