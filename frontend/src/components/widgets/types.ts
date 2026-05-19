import { type ComponentType, type ReactNode } from "react";
import { type StepField } from "../../lib/api";

/**
 * Props passed to a widget's React component.
 *
 * Generic on the widget's value shape — each widget declares the shape
 * of the value it produces (`{ start, end }` for the distribution
 * filter, `{ s3_key, row_count }` for a CSV uploader, etc.). The form
 * stores and submits this value verbatim, transformed by `beforeSubmit`
 * if defined.
 */
export interface WidgetProps<TValue = unknown> {
  /** The StepField schema entry for this widget. */
  field: StepField;
  /** The full XCom payload from the upstream task. */
  xcom: Record<string, unknown>;
  /** Current value from react-hook-form (undefined before first interaction). */
  value: TValue | undefined;
  /** Callback to update the form value. */
  onChange: (value: TValue) => void;
  /** Validation error message, if any. */
  error?: string;
}

/**
 * A widget bundles everything related to one widget kind:
 *   - the interactive component
 *   - how to display a submitted value in the success summary
 *   - optional client-side validation
 *   - optional pre-submission hook (uploads, transformations, etc.)
 *
 * One widget per file. Export the bundle as a const; register it in
 * registry.ts by adding one entry to the registry map.
 */
export interface Widget<TValue = unknown> {
  /** The interactive form component. */
  Component: ComponentType<WidgetProps<TValue>>;

  /**
   * How to display the submitted value in the post-submit summary.
   * Called by HitlNode when rendering the SubmittedEntry view.
   *
   * Return any ReactNode — a string, JSX with custom layout, etc.
   */
  renderSubmitted: (value: TValue, field: StepField) => ReactNode;

  /**
   * Optional client-side validation. Returns null when the value is
   * valid; otherwise an error message string. Runs after Zod schema
   * validation in DynamicForm. The error is attached to the field via
   * react-hook-form's setError.
   */
  validate?: (value: TValue | undefined, field: StepField) => string | null;

  /**
   * Optional pre-submission hook. Runs after validation but before the
   * form's onSubmit fires. Can do async work (uploads, API calls) and
   * can transform the value — the returned value replaces what was in
   * form state for the rest of the submission. Throwing aborts the
   * submission and surfaces the error in the form.
   *
   * The submit button shows loading during this hook.
   *
   * Common use case — CSV upload widget:
   *
   *   beforeSubmit: async (value, field) => {
   *     if (value.uploadedKey) return value;             // already done
   *     const key = await uploadToBackend(value.file);   // do the work
   *     return { ...value, uploadedKey: key };           // swap value
   *   }
   */
  beforeSubmit?: (
    value: TValue | undefined,
    field: StepField,
  ) => Promise<TValue | undefined>;
}
