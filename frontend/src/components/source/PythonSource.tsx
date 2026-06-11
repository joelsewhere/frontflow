import { Highlight, type PrismTheme } from "prism-react-renderer";

/**
 * Syntax-highlighted, read-only Python source viewer for the
 * Source tab on FormSummaryPage and SubmissionDetailPage.
 *
 * Uses prism-react-renderer (Prism + React) — small enough that
 * lazy-loading it isn't worth the wiring, and the Source tab is
 * the only consumer right now. If we add more code-display
 * surfaces later this can stay as a single shared component.
 *
 * The theme is tuned to fit frontflow's existing
 * dark-text-on-light-card aesthetic: keywords blue, strings
 * green-ish, comments muted, numbers/builtins distinct enough to
 * scan. We pass `theme={frontflowTheme}` rather than one of
 * prism's bundled themes so it can pick up the form's themed CSS
 * variables (--ink, --muted, etc.) where possible.
 */
const frontflowTheme: PrismTheme = {
  plain: {
    color: "var(--color-ink)",
    backgroundColor: "transparent",
  },
  styles: [
    // Order matters in Prism — earlier rules win on conflict.
    {
      types: ["comment", "prolog", "doctype", "cdata"],
      style: { color: "var(--color-muted)", fontStyle: "italic" },
    },
    {
      types: ["string", "char", "attr-value", "regex", "variable"],
      // Emerald — readable on both light and dark cards.
      style: { color: "#059669" },
    },
    {
      types: ["keyword", "selector", "important", "atrule"],
      // Blue.
      style: { color: "#2563eb", fontWeight: "600" },
    },
    {
      types: ["function", "class-name"],
      style: { color: "#b45309" },
    },
    {
      types: ["number", "boolean", "constant"],
      style: { color: "#9333ea" },
    },
    {
      types: ["operator", "entity", "url"],
      style: { color: "var(--color-ink)" },
    },
    {
      types: ["punctuation"],
      style: { color: "var(--color-muted)" },
    },
    {
      types: ["builtin", "tag"],
      style: { color: "#0891b2" },
    },
    {
      // `@form`, `@node`, `@listing`, etc. Prism tags decorators
      // as "decorator" but some Python tokenizers emit them as
      // "annotation" — handle both.
      types: ["decorator", "annotation"],
      style: { color: "#0891b2", fontWeight: "600" },
    },
  ],
};

export function PythonSource({ source }: { source: string }) {
  return (
    <Highlight code={source} language="python" theme={frontflowTheme}>
      {({ className, style, tokens, getLineProps, getTokenProps }) => (
        <pre
          className={[
            className,
            // Card-style container matching other read-only blocks.
            "overflow-auto rounded border border-border bg-card p-4 text-[12px] leading-relaxed",
          ].join(" ")}
          style={style}
        >
          <code className="font-mono">
            {tokens.map((line, i) => (
              <div key={i} {...getLineProps({ line })}>
                {line.map((token, j) => (
                  <span key={j} {...getTokenProps({ token })} />
                ))}
              </div>
            ))}
          </code>
        </pre>
      )}
    </Highlight>
  );
}
