/**
 * Node rendering. Two entry points:
 *   - <NodeForm>      — renders a layout tree as an interactive form
 *                       (react-hook-form context; buttons submit).
 *   - <SubmittedTree> — renders a layout tree read-only, with the
 *                       submitted values and the clicked button.
 */

import { useEffect, useMemo, useState } from "react";
import { FormProvider, useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { type Block, type StepResponse } from "../../lib/api";
import { BlockTree } from "./BlockTree";
import {
  BlockRenderContext,
  NodeFormContext,
  type NodeFormContextValue,
} from "./types";
import {
  buildDefaults,
  buildSchema,
  collectButtons,
  collectFields,
  synthWidgetField,
} from "./schema";
import { evalConditions } from "./conditions";
import { getWidget } from "../widgets/registry";

interface NodeFormProps {
  layout: Block;
  /** Id of the node being rendered — for resolving same-node templates. */
  nodeId: string;
  /** Id of the form — for file-upload fields' upload target. */
  formId: string;
  /** Current submission id, if any — for S3File key templating. */
  submissionId: string | null;
  /** Seed values for the form, supplied when a step is re-opened for
   *  editing. Each field falls back to its own default when absent. */
  initialValues?: Record<string, unknown>;
  /** Called with the validated values and the clicked button's id. */
  onSubmit: (values: Record<string, unknown>, button: string | null) => void;
  isSubmitting: boolean;
  error?: string | null;
}

export function NodeForm({
  layout,
  nodeId,
  formId,
  submissionId,
  initialValues,
  onSubmit,
  isSubmitting,
  error,
}: NodeFormProps) {
  const fields = useMemo(() => collectFields(layout), [layout]);
  const schema = useMemo(() => buildSchema(fields), [fields]);
  const defaults = useMemo(
    () => ({ ...buildDefaults(fields), ...(initialValues ?? {}) }),
    [fields, initialValues],
  );
  // The first button is the form's default submit target — what Enter
  // (implicit form submission) activates.
  const primaryButtonId = useMemo(() => {
    const buttons = collectButtons(layout);
    // An id-less button (an unbound `Button()` in source) yields "" —
    // send null so the backend's single-button fallback resolves it.
    const id = buttons[0]?.id;
    return id ? id : null;
  }, [layout]);

  const methods = useForm({
    resolver: zodResolver(schema),
    defaultValues: defaults,
  });

  const [pendingButton, setPendingButton] = useState<string | null>(null);

  const submitWith = (buttonId: string | null) => {
    // Guard against double-submit (Enter mashed during a submit).
    if (isSubmitting) return;
    methods.handleSubmit(
      (values) => {
        // Zod covers required-ness; widget bundles add their own checks.
        for (const f of fields) {
          if (f.type !== "histogram_widget") continue;
          const widget = getWidget("distribution_filter");
          if (!widget?.validate) continue;
          const msg = widget.validate(values[f.id], synthWidgetField(f.raw));
          if (msg) {
            methods.setError(f.id, { type: "widget", message: msg });
            return;
          }
        }
        // Drop values of fields hidden by an unsatisfied `When` — a
        // field the user can't see shouldn't appear in the payload.
        const payload: Record<string, unknown> = { ...values };
        for (const f of fields) {
          if (
            f.conditions.length > 0 &&
            !evalConditions(f.conditions, values as Record<string, unknown>)
          ) {
            delete payload[f.id];
          }
        }
        setPendingButton(buttonId);
        onSubmit(payload, buttonId);
      },
      () => setPendingButton(null),
    )();
  };

  const ctx: NodeFormContextValue = {
    submitWith,
    pendingButton,
    isSubmitting,
  };

  return (
    <FormProvider {...methods}>
      <NodeFormContext.Provider value={ctx}>
        <BlockRenderContext.Provider
          value={{ mode: "form", values: {}, clickedButton: null, nodeId, formId, submissionId }}
        >
          <form
            onSubmit={(e) => {
              // Enter in a field triggers implicit submission — route it
              // through the form's default (first) button.
              e.preventDefault();
              submitWith(primaryButtonId);
            }}
            noValidate
          >
            <BlockTree block={layout} />
            {/* A real (but visually hidden) submit button so the browser
                performs implicit submission on Enter. Not tabbable and
                not clickable — the visible buttons handle clicks. */}
            <button
              type="submit"
              tabIndex={-1}
              aria-hidden
              disabled={isSubmitting}
              className="sr-only"
            />
          </form>
          {error ? (
            <p className="mt-4 text-sm text-error">{error}</p>
          ) : null}
        </BlockRenderContext.Provider>
      </NodeFormContext.Provider>
    </FormProvider>
  );
}

export function SubmittedTree({
  layout,
  nodeId,
  formId,
  submissionId,
  response,
}: {
  layout: Block;
  nodeId: string;
  formId: string;
  submissionId: string | null;
  response: StepResponse | null;
}) {
  const values = useMemo(
    () => response?.values ?? {},
    [response],
  );
  // A read-only form context seeded with the submitted values, so the
  // block tree's useWatch() calls — `When` visibility and live label
  // templating — resolve uniformly with form mode. Never edited.
  const methods = useForm({ defaultValues: values });
  useEffect(() => {
    methods.reset(values);
  }, [values, methods]);

  return (
    <FormProvider {...methods}>
      <BlockRenderContext.Provider
        value={{
          mode: "submitted",
          values,
          clickedButton: response?.button ?? null,
          nodeId,
          formId,
          submissionId,
        }}
      >
        <BlockTree block={layout} />
      </BlockRenderContext.Provider>
    </FormProvider>
  );
}
