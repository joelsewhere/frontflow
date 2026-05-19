import { forwardRef, type InputHTMLAttributes } from "react";
import { Field } from "./Field";

interface TextFieldProps extends InputHTMLAttributes<HTMLInputElement> {
  label: string;
  error?: string;
  hint?: string;
}

/**
 * Text input field. Uncoupled from any form library — accepts standard
 * input props plus label/error/hint. To use with react-hook-form, just
 * spread `{...register("fieldName")}`.
 *
 *   <TextField label="Email" error={errors.email?.message} {...register("email")} />
 */
export const TextField = forwardRef<HTMLInputElement, TextFieldProps>(
  function TextField({ label, error, hint, id, name, ...rest }, ref) {
    const inputId = id ?? name;
    return (
      <Field label={label} htmlFor={inputId} error={error} hint={hint}>
        <input
          ref={ref}
          id={inputId}
          name={name}
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
