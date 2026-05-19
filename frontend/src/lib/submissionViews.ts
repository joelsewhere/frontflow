import type { TaskInstance } from "./api";

/**
 * A submission renders one **view** at a time (design choice B). A view
 * is a maximal run of consecutive chain tasks sharing the same page:
 *
 *   - `kind: "page"`  — a `@page`; the user is navigated *into* it and
 *     sees only its section nodes.
 *   - `kind: "flow"`  — a run of top-level nodes / backend steps that
 *     belong to no page (`page_id` null). A single-page form is one
 *     flow view from beginning to end.
 *
 * Back/forward navigate between views; within a view the tasks render
 * as a scoped chain.
 */
export interface SubmissionView {
  kind: "page" | "flow";
  /** Stable key for React reconciliation. */
  key: string;
  /** URL path segment identifying this view — the page id for a page,
   *  the lead node id for a flow view. */
  viewId: string;
  /** Page id / title — present only for `kind: "page"`. */
  pageId: string | null;
  pageTitle: string | null;
  /** The chain tasks belonging to this view, in order. */
  tasks: TaskInstance[];
}

/**
 * Split a submission's flat task list into ordered views. A page's
 * tasks are always contiguous (a page is traversed fully before the
 * workflow moves on), so a maximal same-`page_id` run is exactly one
 * page; all page-less tasks between pages collapse into flow views.
 */
export function buildSubmissionViews(
  tasks: TaskInstance[],
): SubmissionView[] {
  const views: SubmissionView[] = [];

  for (const task of tasks) {
    const pageId = task.page_id;
    const last = views[views.length - 1];

    // Extend the current view when the page id matches (null === null
    // groups consecutive page-less tasks into one flow view).
    if (last && last.pageId === pageId) {
      last.tasks.push(task);
      continue;
    }

    views.push(
      pageId !== null
        ? {
            kind: "page",
            key: `page:${pageId}`,
            viewId: pageId,
            pageId,
            pageTitle: task.page_title,
            tasks: [task],
          }
        : {
            kind: "flow",
            key: `flow:${task.task_id}`,
            // A flow view is identified by its lead node — the first
            // page-less task always being the node that opens the run.
            viewId: task.task_id,
            pageId: null,
            pageTitle: null,
            tasks: [task],
          },
    );
  }

  return views;
}
