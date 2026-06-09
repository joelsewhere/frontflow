import { Link } from "react-router-dom";
import { DagNode } from "./DagNode";
import { ResetButton } from "./ResetButton";
import { NodeForm, SubmittedTree } from "../blocks/NodeForm";
import { collectButtons } from "../blocks/schema";
import { type CascadeStatus, type AssignedChild } from "../../lib/api";
import { useStepDetail } from "../../hooks/useStepDetail";
import { useSubmitStep } from "../../hooks/useSubmitStep";
import { formatTimestamp } from "../../lib/format";

interface HitlNodeProps {
  formId: string;
  submissionId: string;
  stepId: string;
  stepLabel?: string;
  /** Edit-cascade status from the submission's task list. */
  cascadeStatus?: CascadeStatus;
  /** Children spawned by an Assign on this task. Empty / undefined when
   *  the operator didn't fire any Assign or no grants were produced.
   *  Rendered as a chip list under SubmittedTree so the user (or admin
   *  viewing the live form) can jump into the spawned child submission
   *  and see the running grant state. */
  assignments?: AssignedChild[];
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
  assignments,
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

  // A chain step downstream of this node's submit failed (a backend
  // that raised, an operator that errored). The user submitted the
  // form, but the chain didn't make it. Render the step as failed and
  // show the error — *don't* fall through to the "submitted" branch,
  // which would lie about the state of the world.
  if (data.error) {
    return (
      <DagNode
        status="failed"
        stepLabel={stepLabel}
        cascadeStatus={status}
        title={title}
        subtitle="failed"
        headerAction={
          <ResetButton
            formId={formId}
            submissionId={submissionId}
            fromTaskId={data.step_id}
            variant="link"
            allowEdit
          />
        }
      >
        <div className="border border-error bg-surface p-3 text-sm font-mono text-error whitespace-pre-wrap break-words">
          {data.error}
        </div>
        <SubmittedTree
          layout={data.layout}
          nodeId={data.step_id}
          formId={formId}
          submissionId={submissionId}
          response={data.response}
        />
        {assignments && assignments.length > 0 ? (
          <AssignmentsList assignments={assignments} />
        ) : null}
      </DagNode>
    );
  }

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
          isCompletion ? undefined : (
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
        {assignments && assignments.length > 0 ? (
          <AssignmentsList assignments={assignments} />
        ) : null}
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

/** Chip list of every SubmissionAssignment row this step's Assign
 *  operator produced. Mirrors the AssignmentsList rendered in the
 *  submission summary — same structure, same fields — but lives
 *  inline next to the live HITL node so the user can jump into a
 *  spawned child without bouncing through the summary view. Empty
 *  list / undefined → not rendered. */
function AssignmentsList({ assignments }: { assignments: AssignedChild[] }) {
  return (
    <div className="mt-4 border border-border bg-surface px-3 py-3">
      <p className="mb-2 font-mono text-[10px] uppercase tracking-wider text-muted">
        Assigned ({assignments.length})
      </p>
      <ul className="space-y-1.5">
        {assignments.map((a) => {
          const subId = a.child_submission_id ?? a.child_submission_handle;
          const href = `/forms/${encodeURIComponent(a.child_form_id)}/submissions/${encodeURIComponent(subId)}`;
          const isRevoked = a.revoked_at !== null;
          return (
            <li
              key={a.assignment_id}
              className="flex items-baseline justify-between gap-3 text-xs"
            >
              <div className="min-w-0 flex-1">
                <Link
                  to={href}
                  className="font-medium text-ink hover:text-accent"
                >
                  {a.child_form_title}
                </Link>
                <span className="ml-2 font-mono text-[10px] uppercase tracking-wider text-muted">
                  {a.role_id}
                </span>
                {a.assignee_username ? (
                  <span className="ml-2 font-mono text-[11px] text-muted">
                    → {a.assignee_username}
                  </span>
                ) : null}
              </div>
              <div className="flex shrink-0 items-center gap-2 font-mono text-[10px] uppercase tracking-wider">
                {isRevoked ? (
                  <span className="text-muted">revoked</span>
                ) : (
                  <span
                    className={
                      a.child_submission_state === "success"
                        ? "text-muted"
                        : a.child_submission_state === "failed"
                          ? "text-error"
                          : "text-accent"
                    }
                  >
                    {a.child_submission_state}
                  </span>
                )}
                <span className="text-muted">
                  {formatTimestamp(a.granted_at)}
                </span>
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
