/**
 * Sanitising a rendered data story before it goes into the page.
 *
 * A story is author-produced, and an author who can write one can
 * already run Python on the server — so sanitising is not protection
 * from the author. It is protection from the DATA: a cell that prints a
 * value someone typed into a form bakes that value into the HTML, and
 * the person who then opens the story is usually an administrator. That
 * is a stored-XSS path from any form field to an admin's session, and
 * it exists no matter how much the author is trusted.
 *
 * Consequence worth knowing: scripts are stripped, so an xmd `ojs` cell
 * — whose output is interactive JavaScript — renders as inert markup.
 * Executed Python, R and Markdown output is unaffected, because xmd has
 * already baked those to static HTML at render time.
 */

/** Tags xmd emits that must survive, beyond DOMPurify's defaults. */
export const ALLOWED_ATTR = ["class", "data-lang", "href", "src", "alt", "title"];

export const PURIFY_CONFIG = {
  ALLOWED_ATTR,
  // Keep the document a fragment: a story is embedded in a panel, not
  // loaded as a page.
  WHOLE_DOCUMENT: false,
  RETURN_DOM: false,
  RETURN_DOM_FRAGMENT: false,
} as const;

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
