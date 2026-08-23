/**
 * Presenting a rendered data story.
 *
 * A story is NOT sanitised and inlined into the app. It is served as its
 * own document and framed in a sandbox, so the author can write whatever
 * HTML, CSS and JavaScript they like — their own libraries included —
 * and none of it can reach frontflow.
 *
 * That is a deliberate reversal. Sanitising was protecting the app from
 * the story's DATA (a cell printing a form-submitted value is a path
 * from any form field to an admin's session), but it did so by
 * destroying the thing a story is for: an author's own page. Isolation
 * solves the same problem without the cost — the document gets an
 * opaque origin, so what it contains stops mattering.
 *
 * The sandbox is applied as a response header on the page route, not
 * merely as an iframe attribute, so opening the URL directly is
 * isolated too. See STORY_SANDBOX in main.py.
 *
 * What remains here is what the PANEL decides: whether there is
 * anything to show, and what to say above it.
 */

/**
 * Whether a story should be shown at all.
 *
 * An unrendered story has no HTML — the panel says so and names the
 * command, rather than rendering an empty box that looks like a bug.
 */
export function storyState(story: {
  rendered?: boolean;
  html?: string | null;
  stale?: boolean | null;
  cell_errors?: number;
}): "missing" | "ready" {
  if (!story.rendered || !story.html) return "missing";
  return "ready";
}

/**
 * The banner a story needs above it, if any.
 *
 * Staleness is three-valued on purpose: `null` means the artifact
 * carries no header and cannot be checked, which is a weaker claim than
 * "checked, and current" and should not be reported as either.
 */
export function storyNotice(story: {
  stale?: boolean | null;
  cell_errors?: number;
}): { tone: "warning" | "error"; text: string } | null {
  const errors = story.cell_errors ?? 0;
  if (errors > 0) {
    return {
      tone: "error",
      text:
        errors === 1
          ? "1 cell failed when this was rendered."
          : `${errors} cells failed when this was rendered.`,
    };
  }
  if (story.stale === true) {
    return {
      tone: "warning",
      text: "Rendered from an older version of the source.",
    };
  }
  return null;
}
