import { DagNode } from "./DagNode";
import { ResetButton } from "./ResetButton";
import { NodeForm, SubmittedTree } from "../blocks/NodeForm";
import { collectButtons } from "../blocks/schema";
import { type CascadeStatus } from "../../lib/api";
import { useStepDetail } from "../../hooks/useStepDetail";
import { useSubmitStep } from "../../hooks/useSubmitStep";

interface HitlNodeProps {
  formId: string;
  submissionId: string;
  stepId: string;
  stepLabel?: string;
  /** Edit-cascade status from the submission's task list. */
  cascadeStatus?: CascadeStatus;
}

/** Humanize a node id for the chain card title ("review_summary" →
 *  "Review summary"). */
function humanize(id: string): string {
  const s = id.replace(/_/g, " ").trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/**
 * Node for any HITL step. Renders the step's layout tree — as an
 * interactive form while awaiting input, read-only once submitted.
 */
export function HitlNode({
  formId,
  submissionId,
  stepId,
  stepLabel,
  cascadeStatus,
}: HitlNodeProps) {
  const { data, error, isLoading } = useStepDetail(formId, submissionId, stepId);
  const submitMutation = useSubmitStep(formId, submissionId, stepId);

  if (isLoading) {
    return (
      <DagNode status="waiting" stepLabel={stepLabel} title="Loading…">
        <p className="text-sm text-muted">Fetching step…</p>
      </DagNode>
    );
  }

  if (error || !data) {
    return (
      <DagNode
        status="failed"
        stepLabel={stepLabel}
        title="Couldn't load step"
      >
        <p className="text-sm text-error">
          {error?.message ?? "Unknown error"}
        </p>
      </DagNode>
    );
  }

  const title = humanize(data.step_id);
  // The step-detail status is authoritative (freshest); fall back to
  // the task-list status passed in.
  const status: CascadeStatus = data.status ?? cascadeStatus ?? "unaffected";

  if (data.response_received) {
    // A buttonless node is the workflow's final screen — it completed
    // on arrival, not by a submit. Render it as a plain completion:
    // no "submitted" line, no per-step reset (there's nothing after it).
    const isCompletion = collectButtons(data.layout).length === 0;
    return (
      <DagNode
        status="success"
        stepLabel={stepLabel}
        cascadeStatus={status}
        title={title}
        subtitle={isCompletion ? "complete" : "submitted"}
        headerAction={
          data.is_landing || isCompletion ? undefined : (
            <ResetButton
              formId={formId}
              submissionId={submissionId}
              fromTaskId={data.step_id}
              variant="link"
              allowEdit
            />
          )
        }
      >
        <SubmittedTree layout={data.layout} nodeId={data.step_id} formId={formId} submissionId={submissionId} response={data.response} />
      </DagNode>
    );
  }

  // An awaiting step the user is actively editing gets a Cancel — it
  // reverts the edit by re-submitting the held draft answers unchanged
  // (the cascade then no-ops). Cancel is a clean inverse only here,
  // before the edit is committed.
  const cancelEdit =
    data.edit_in_progress && data.draft
      ? () =>
          submitMutation.mutate({
            values: data.draft!.values,
            button: data.draft!.button ?? null,
          })
      : null;

  return (
    <DagNode
      status="waiting"
      stepLabel={stepLabel}
      cascadeStatus={status}
      title={title}
      subtitle={`awaiting input · ${data.step_id}`}
      headerAction={
        cancelEdit ? (
          <button
            type="button"
            onClick={cancelEdit}
            disabled={submitMutation.isPending}
            className="text-[10px] uppercase tracking-[0.2em] text-muted hover:text-ink underline underline-offset-2 disabled:opacity-50"
          >
            Cancel edit
          </button>
        ) : undefined
      }
    >
      <NodeForm
        layout={data.layout}
        nodeId={data.step_id}
        formId={formId}
        submissionId={submissionId}
        initialValues={data.draft?.values}
        isSubmitting={submitMutation.isPending}
        error={submitMutation.error?.message ?? null}
        onSubmit={(values, button) =>
          submitMutation.mutate({ values, button })
        }
      />
    </DagNode>
  );
}
