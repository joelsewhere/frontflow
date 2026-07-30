import { type ReactNode } from "react";
import { type NodeStatus } from "../../lib/dagState";
import { type CascadeStatus } from "../../lib/api";
import { StatusIndicator } from "./StatusIndicator";
import { CommentToggle } from "../comments/CommentThread";

interface DagNodeProps {
  status: NodeStatus;
  title: string;
  subtitle?: string;
  stepLabel?: string; // e.g. "STEP 02"
  /**
   * Edit-cascade status. When "needs_review" or "needs_input", a badge
   * is shown — the step was touched by an upstream edit.
   */
  cascadeStatus?: CascadeStatus;
  /**
   * Optional action rendered top-right (typically a small button or
   * link — e.g., a reset action for a submitted HITL). Aligned with the
   * step label so the visual hierarchy stays clean.
   */
  headerAction?: ReactNode;
  /** When set, the card header carries a comment toggle opening the
   *  node-level thread (Google-Docs-style discussion per node). */
  commentThread?: { formId: string; submissionId: string; threadId: string };
  children?: ReactNode;
}

/** A small tag flagging how an upstream edit left this step. */
function CascadeBadge({ status }: { status: CascadeStatus }) {
  if (status === "unaffected") return null;
  const isInput = status === "needs_input";
  return (
    <span
      className={[
        "font-mono text-[10px] uppercase tracking-[0.18em] px-2 py-0.5 border",
        isInput
          ? "text-error border-error"
          : "text-accent border-accent",
      ].join(" ")}
    >
      {isInput ? "Needs input" : "Needs review"}
    </span>
  );
}

/**
 * The shared visual shell for every node in the DAG chain. Concretely a
 * bordered card with:
 *
 *   ┌─────────────────────────────────────────┐
 *   │ STEP 02                       [action]  │  ← stepLabel + headerAction
 *   │ ⊙  Processing submission                │  ← indicator + title
 *   │    fetching records · 6.3s              │  ← subtitle
 *   │ ───────────────────────────────────────  │
 *   │  [children: task list, form, anything]  │
 *   └─────────────────────────────────────────┘
 *
 * The shell knows nothing about its content — pass any ReactNode as
 * children. Specialized node compositions (ProgressNode, HitlNode,
 * BackendStepNode) wrap this with content + behavior.
 */
export function DagNode({
  status,
  title,
  subtitle,
  stepLabel,
  cascadeStatus,
  headerAction,
  commentThread,
  children,
}: DagNodeProps) {
  const badge =
    cascadeStatus && cascadeStatus !== "unaffected" ? (
      <CascadeBadge status={cascadeStatus} />
    ) : null;
  return (
    <article
      className={[
        "bg-surface border rounded-theme",
        status === "failed" ? "border-error" : "border-border",
      ].join(" ")}
      data-status={status}
    >
      <header className="px-6 pt-5 pb-4 flex flex-col gap-3">
        {stepLabel || headerAction || badge || commentThread ? (
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-3 min-w-0">
              {stepLabel ? (
                <p className="font-mono text-[10px] uppercase tracking-[0.25em] text-muted">
                  {stepLabel}
                </p>
              ) : (
                <span />
              )}
              {badge}
            </div>
            <div className="flex items-center gap-2">
              {commentThread ? (
                <CommentToggle
                  formId={commentThread.formId}
                  submissionId={commentThread.submissionId}
                  threadId={commentThread.threadId}
                  label="Node discussion"
                />
              ) : null}
              {headerAction ? <div>{headerAction}</div> : null}
            </div>
          </div>
        ) : null}
        <div className="flex items-start gap-3">
          <span className="mt-1">
            <StatusIndicator status={status} />
          </span>
          <div className="flex flex-col gap-1 min-w-0 flex-1">
            <h2 className="font-display text-xl font-semibold leading-tight text-ink">
              {title}
            </h2>
            {subtitle ? (
              <p className="text-sm text-muted">{subtitle}</p>
            ) : null}
          </div>
        </div>
      </header>
      {children ? (
        <div className="border-t border-border px-6 py-5">{children}</div>
      ) : null}
    </article>
  );
}
