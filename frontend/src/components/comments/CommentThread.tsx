import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { getComments, postComment } from "../../lib/api";

/**
 * One comment thread — list + composer — scoped to
 * (form, submission, thread id). The shared engine behind every
 * comment surface: the standalone `displays.Comments` block, the
 * per-component anchor (`.with_comments()`), and the node-level
 * thread on chain cards.
 */
export function CommentThread({
  formId,
  submissionId,
  threadId,
  label,
  placeholder = "Add a comment…",
}: {
  formId: string;
  submissionId: string;
  threadId: string;
  label?: string | null;
  placeholder?: string;
}) {
  const [draft, setDraft] = useState("");
  const queryClient = useQueryClient();
  const queryKey = ["comments", formId, submissionId, threadId];

  const { data: comments } = useQuery({
    queryKey,
    queryFn: () => getComments(formId, submissionId, threadId),
    refetchInterval: 15_000,
  });
  const post = useMutation({
    mutationFn: () => postComment(formId, submissionId, threadId, draft.trim()),
    onSuccess: () => {
      setDraft("");
      queryClient.invalidateQueries({ queryKey });
    },
  });

  return (
    <div className="flex flex-col gap-2">
      {label ? (
        <span className="text-[10px] uppercase tracking-[0.2em] text-muted font-mono">
          {label}
        </span>
      ) : null}
      <div className="flex flex-col gap-2.5">
        {(comments ?? []).map((c) => (
          <div key={c.id} className="flex flex-col gap-0.5">
            <div className="flex items-baseline gap-2">
              <span className="font-mono text-xs text-ink">{c.author}</span>
              <span className="font-mono text-[10px] text-muted">
                {new Date(c.created_at).toLocaleString()}
              </span>
            </div>
            <p className="text-sm text-ink whitespace-pre-wrap break-words m-0">
              {c.body}
            </p>
          </div>
        ))}
        {comments && comments.length === 0 ? (
          <p className="text-xs text-muted m-0">No comments yet.</p>
        ) : null}
      </div>
      <div className="flex gap-2 items-end">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={placeholder}
          rows={2}
          className="flex-1 border border-border bg-bg px-2.5 py-1.5 text-sm text-ink resize-y min-h-[2.25rem]"
        />
        <button
          type="button"
          onClick={() => post.mutate()}
          disabled={!draft.trim() || post.isPending}
          className="border border-border px-3 py-1.5 font-mono text-xs uppercase tracking-wider text-muted hover:text-ink disabled:opacity-40"
        >
          {post.isPending ? "Posting…" : "Post"}
        </button>
      </div>
      {post.error ? (
        <p className="text-xs text-error m-0">Couldn't post the comment.</p>
      ) : null}
    </div>
  );
}

/** Minimal monochrome speech-bubble glyph. */
function BubbleIcon({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 16 16"
      width="14"
      height="14"
      className={className}
      aria-hidden
    >
      <path
        d="M2 3.5A1.5 1.5 0 0 1 3.5 2h9A1.5 1.5 0 0 1 14 3.5v6a1.5 1.5 0 0 1-1.5 1.5H8l-3.5 3v-3h-1A1.5 1.5 0 0 1 2 9.5v-6Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/**
 * The comment toggle: a bubble button with a live count that opens a
 * thread panel. `panelMode` picks the open-state presentation:
 * "floating" overlays a card (component anchors, Google-Docs style);
 * "inline" expands in place (node cards).
 */
export function CommentToggle({
  formId,
  submissionId,
  threadId,
  label,
  panelMode = "floating",
}: {
  formId: string;
  submissionId: string;
  threadId: string;
  label?: string | null;
  panelMode?: "floating" | "inline";
}) {
  const [open, setOpen] = useState(false);
  const { data: comments } = useQuery({
    queryKey: ["comments", formId, submissionId, threadId],
    queryFn: () => getComments(formId, submissionId, threadId),
    refetchInterval: 30_000,
  });
  const count = comments?.length ?? 0;

  const button = (
    <button
      type="button"
      onClick={() => setOpen((o) => !o)}
      aria-expanded={open}
      aria-label={`Comments (${count})`}
      className={[
        "inline-flex items-center gap-1 px-1.5 py-1 font-mono text-[10px]",
        "border transition-colors",
        open || count > 0
          ? "border-border text-ink bg-surface"
          : "border-transparent text-muted hover:text-ink hover:border-border",
      ].join(" ")}
    >
      <BubbleIcon />
      {count > 0 ? <span>{count}</span> : null}
    </button>
  );

  const panel = open ? (
    <div
      className={
        panelMode === "floating"
          ? "absolute right-0 top-full mt-1 z-20 w-80 max-h-96 overflow-auto border border-border bg-surface shadow-lg p-3"
          : "border border-border bg-bg p-3 mt-2"
      }
    >
      <CommentThread
        formId={formId}
        submissionId={submissionId}
        threadId={threadId}
        label={label ?? "Comments"}
      />
    </div>
  ) : null;

  if (panelMode === "inline") {
    return (
      <div className="flex flex-col">
        <div className="self-end">{button}</div>
        {panel}
      </div>
    );
  }
  return (
    <div className="relative inline-block">
      {button}
      {panel}
    </div>
  );
}
