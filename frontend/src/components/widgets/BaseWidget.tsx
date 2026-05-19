import { type ReactNode } from "react";

interface BaseWidgetProps {
  /** Field label, shown above the widget content. */
  label: string;
  /** Optional hint text shown below the content when no error is present. */
  hint?: string;
  /** Validation error message; takes precedence over hint. */
  error?: string;
  /** Widget-specific content. */
  children: ReactNode;
}

/**
 * The shared field shell every widget composes. Provides the label on
 * top, the widget content in the middle, and the error or hint below.
 *
 * Use this as the outermost element of any new widget. It keeps the
 * label typography, spacing, and error styling consistent across all
 * widgets without each one re-implementing the wrapper.
 *
 *   export function MyWidget({ field, value, onChange, error }: WidgetProps<MyValue>) {
 *     return (
 *       <BaseWidget label={field.label} error={error} hint="Pick an option below">
 *         {/* widget-specific content here *\/}
 *       </BaseWidget>
 *     );
 *   }
 */
export function BaseWidget({ label, hint, error, children }: BaseWidgetProps) {
  return (
    <div className="flex flex-col gap-2">
      <span className="text-xs uppercase tracking-[0.18em] text-muted font-medium">
        {label}
      </span>
      {children}
      {error ? (
        <p className="text-sm text-error">{error}</p>
      ) : hint ? (
        <p className="text-sm text-muted">{hint}</p>
      ) : null}
    </div>
  );
}
