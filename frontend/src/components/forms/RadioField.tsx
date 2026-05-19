import { forwardRef, type InputHTMLAttributes } from "react";
import { Field } from "./Field";

interface RadioFieldProps extends InputHTMLAttributes<HTMLInputElement> {
  label: string;
  options: readonly string[];
  error?: string;
  hint?: string;
}

/**
 * Radio-button group — one choice from a fixed list, every option
 * visible at once. Spread `register()` — including its `ref` — onto
 * *every* radio input: react-hook-form tracks a radio group by reading
 * the checked input out of the full set of refs, so a ref on only the
 * first radio leaves it unable to see the others' selections.
 */
export const RadioField = forwardRef<HTMLInputElement, RadioFieldProps>(
  function RadioField({ label, options, error, hint, name, ...rest }, ref) {
    return (
      <Field label={label} error={error} hint={hint}>
        <div className="flex flex-col gap-2.5">
          {options.map((opt) => (
            <label
              key={opt}
              className="inline-flex items-center gap-3 cursor-pointer select-none"
            >
              <input
                ref={ref}
                type="radio"
                name={name}
                value={opt}
                {...rest}
                className="w-4 h-4 accent-ink cursor-pointer"
              />
              <span className="text-base text-ink font-sans">{opt}</span>
            </label>
          ))}
        </div>
      </Field>
    );
  },
);
