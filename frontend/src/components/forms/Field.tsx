import { type ReactNode } from "react";

interface FieldProps {
  label: string;
  htmlFor?: string;
  error?: string;
  hint?: string;
  children: ReactNode;
}

/**
 * Base field shell: label on top, input slot below, error/hint underneath.
 * Holds no input state itself — input components are passed as children.
 * This is the layout primitive every form field will share.
 */
export function Field({ label, htmlFor, error, hint, children }: FieldProps) {
  return (
    <div className="flex flex-col gap-2">
      <label
        htmlFor={htmlFor}
        className="text-xs uppercase tracking-[0.18em] text-muted font-medium"
      >
        {label}
      </label>
      {children}
      {error ? (
        <p className="text-sm text-error font-sans">{error}</p>
      ) : hint ? (
        <p className="text-sm text-muted font-sans">{hint}</p>
      ) : null}
    </div>
  );
}
