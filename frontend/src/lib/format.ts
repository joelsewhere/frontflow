/**
 * Small formatting helpers for the listing tables.
 */

/**
 * Compact, locale-stable timestamp for tracking tables — renders as
 * e.g. "16 May 2026, 14:30". Returns an em dash for null / unparseable
 * input so table cells always have something to show.
 */
export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const date = d.toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
  const time = d.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
  });
  return `${date}, ${time}`;
}
