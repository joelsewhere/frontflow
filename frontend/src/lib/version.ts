/**
 * Format a (major, minor) form-version pair for display.
 *
 *  - `v{major}` when minor is 0 (the common case — preserves the
 *    legacy display so forms that have never had a minor edit look
 *    unchanged).
 *  - `v{major}.{minor}` when minor > 0.
 *
 * Used by every place in the UI that renders a form version. Keeping
 * the rule in one place avoids "v1.0" leaking through anywhere a
 * developer forgot the suppression check.
 */
export function formatVersion(major: number, minor: number = 0): string {
  return minor > 0 ? `v${major}.${minor}` : `v${major}`;
}

/**
 * Compare two (major, minor) version pairs lexicographically.
 * Returns negative / zero / positive — same shape as `Array.sort`'s
 * comparator contract.
 *
 * Used to decide whether `live` is newer than `pinned`. Direct field
 * comparison is too easy to get wrong when minor enters the picture
 * — `live.major > pinned.major OR (live.major == pinned.major AND
 * live.minor > pinned.minor)` is the rule and this helper is the
 * single place that encodes it.
 */
export function compareVersion(
  a: { major: number; minor: number },
  b: { major: number; minor: number },
): number {
  if (a.major !== b.major) return a.major - b.major;
  return a.minor - b.minor;
}
