import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError, respondToHitl, type TaskInstance } from "../../lib/api";
import { DagNode } from "./DagNode";

interface AirflowHitlNodeProps {
  task: TaskInstance;
  formId: string;
  submissionId: string;
  stepLabel?: string;
}

/** Humanize a task id for the card title ("approve_report" →
 *  "Approve report"). */
function humanize(id: string): string {
  const s = id.replace(/_/g, " ").trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/**
 * Node for an Airflow human-in-the-loop task that is awaiting a
 * response. While the DAG is paused, the form becomes the HITL action's
 * response surface: it renders the prompt — subject, body, options, and
 * any parameter fields — and on submit PATCHes the answer back through
 * the backend, which resumes the DAG.
 *
 * A delivery failure is shown inline and the submission is left intact,
 * so the user can simply retry.
 */
export function AirflowHitlNode({
  task,
  formId,
  submissionId,
  stepLabel,
}: AirflowHitlNodeProps) {
  const prompt = task.hitl;
  const queryClient = useQueryClient();

  // Option selection — a set so single- and multi-select share state.
  const [chosen, setChosen] = useState<string[]>(prompt?.defaults ?? []);
  // Param field values, keyed by the param name from Airflow's schema.
  const [params, setParams] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);

  const respond = useMutation({
    mutationFn: () =>
      respondToHitl(formId, submissionId, task.task_id, chosen, params),
    onSuccess: () => {
      // Refresh the submission so the chain advances past the HITL step.
      queryClient.invalidateQueries({
        queryKey: ["submission", formId, submissionId],
      });
    },
    onError: (err) => {
      setFormError(
        err instanceof ApiError
          ? err.message
          : "Couldn't deliver the response — try again.",
      );
    },
  });

  if (!prompt) {
    // Defensive — an awaiting HITL task always carries its prompt.
    return (
      <DagNode status="waiting" stepLabel={stepLabel} title="Awaiting input">
        <p className="text-sm text-muted">Loading the request…</p>
      </DagNode>
    );
  }

  const multiple = prompt.multiple;
  const paramNames = Object.keys(prompt.params);

  function toggleOption(option: string) {
    setFormError(null);
    setChosen((prev) => {
      if (multiple) {
        return prev.includes(option)
          ? prev.filter((o) => o !== option)
          : [...prev, option];
      }
      return [option];
    });
  }

  function handleSubmit() {
    setFormError(null);
    if (chosen.length === 0) {
      setFormError("Choose an option to continue.");
      return;
    }
    respond.mutate();
  }

  return (
    <DagNode
      status="waiting"
      stepLabel={stepLabel}
      cascadeStatus={task.status}
      title={prompt.subject ? prompt.subject : humanize(task.task_id)}
      subtitle={`awaiting response · ${task.task_id}`}
    >
      <div className="flex flex-col gap-5">
        {prompt.body ? (
          <p className="text-sm leading-relaxed text-ink">{prompt.body}</p>
        ) : null}

        {/* Parameter fields — Airflow's param schema for the action.
            Each entry is { default, type, description }. */}
        {paramNames.length > 0 ? (
          <div className="flex flex-col gap-3">
            {paramNames.map((name) => {
              const spec = (prompt.params[name] ?? {}) as {
                default?: unknown;
                type?: string;
                description?: string | null;
              };
              const current =
                params[name] ??
                (spec.default != null ? String(spec.default) : "");
              return (
                <label key={name} className="flex flex-col gap-1.5">
                  <span className="font-mono text-[11px] uppercase tracking-wider text-muted">
                    {humanize(name)}
                  </span>
                  {spec.description ? (
                    <span className="text-xs text-muted">
                      {spec.description}
                    </span>
                  ) : null}
                  <input
                    type={spec.type === "number" ? "number" : "text"}
                    value={current}
                    onChange={(e) =>
                      setParams((p) => ({ ...p, [name]: e.target.value }))
                    }
                    className="w-full rounded-theme border border-border bg-surface px-3 py-2 text-sm text-ink"
                  />
                </label>
              );
            })}
          </div>
        ) : null}

        {/* Options — the answer. Single-select unless `multiple`. */}
        <div className="flex flex-col gap-2">
          <span className="font-mono text-[11px] uppercase tracking-wider text-muted">
            {multiple ? "Select one or more" : "Select a response"}
          </span>
          <div className="flex flex-wrap gap-2">
            {prompt.options.map((option) => {
              const active = chosen.includes(option);
              return (
                <button
                  key={option}
                  type="button"
                  onClick={() => toggleOption(option)}
                  disabled={respond.isPending}
                  className={[
                    "rounded-theme border px-4 py-2 font-sans text-sm transition-colors disabled:opacity-50",
                    active
                      ? "border-ink bg-ink text-bg"
                      : "border-border text-ink hover:border-ink",
                  ].join(" ")}
                >
                  {option}
                </button>
              );
            })}
          </div>
        </div>

        {formError ? (
          <p className="rounded-theme border border-error bg-bg px-3 py-2 text-sm text-error">
            {formError}
          </p>
        ) : null}

        <div>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={respond.isPending}
            className="rounded-theme bg-ink px-5 py-2.5 font-mono text-xs uppercase tracking-wider text-bg transition-colors hover:bg-accent-hover disabled:opacity-50"
          >
            {respond.isPending ? "Submitting…" : "Submit response"}
          </button>
        </div>
      </div>
    </DagNode>
  );
}
