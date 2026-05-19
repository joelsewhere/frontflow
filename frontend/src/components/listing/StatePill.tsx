/**
 * State label — uppercase mono, bordered, sharp-cornered. Used for both
 * submission states (running / success / failed) and step states
 * (awaiting / submitted / failed).
 *
 * Colors come only from theme tokens. The theme has no success/warning
 * color by design, so an active state borrows `accent` and a finished
 * one is neutral `ink`; only a failure gets `error`.
 */

const STATE_STYLES: Record<string, string> = {
  // Submission states.
  running: "text-accent border-accent",
  success: "text-ink border-ink",
  failed: "text-error border-error",
  // Step states.
  awaiting: "text-accent border-accent",
  submitted: "text-ink border-ink",
};

export function StatePill({ state }: { state: string }) {
  const cls = STATE_STYLES[state] ?? "text-muted border-border";
  return (
    <span
      className={`inline-block border px-2 py-0.5 font-mono text-[11px] uppercase tracking-wider ${cls}`}
    >
      {state}
    </span>
  );
}
