import { forwardRef, type TextareaHTMLAttributes } from "react";
import { Field } from "./Field";

interface TextareaFieldProps
  extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  label: string;
  error?: string;
  hint?: string;
}

/**
 * Multi-line text primitive. Same pattern as TextField.
 */
export const TextareaField = forwardRef<
  HTMLTextAreaElement,
  TextareaFieldProps
>(function TextareaField({ label, error, hint, id, name, ...rest }, ref) {
  const inputId = id ?? name;
  return (
    <Field label={label} htmlFor={inputId} error={error} hint={hint}>
      <textarea
        ref={ref}
        id={inputId}
        name={name}
        rows={3}
        {...rest}
        className={[
          "w-full bg-surface border border-border rounded-theme",
          "px-4 py-3 text-base text-ink placeholder:text-muted/60",
          "font-sans resize-y",
          "outline-none transition-colors",
          "focus:border-ink",
          error ? "border-error" : "",
          rest.className ?? "",
        ].join(" ")}
      />
    </Field>
  );
});
