import type { Theme } from "./theme";

/**
 * Build the CSS-variable map for a theme — the same variables
 * `applyTheme` writes to <html>, as a plain object. Used both for the
 * document-level product theme and for per-form themes injected onto a
 * scoping element (inline `style`).
 *
 * Colors are space-separated RGB triplets (e.g. "10 10 10") so
 * Tailwind's <alpha-value> modifier works (e.g. `bg-accent/50`).
 */
export function themeToCssVars(theme: Theme): Record<string, string> {
  return {
    "--color-bg": hexToRgbTriplet(theme.colors.bg),
    "--color-surface": hexToRgbTriplet(theme.colors.surface),
    "--color-ink": hexToRgbTriplet(theme.colors.ink),
    "--color-muted": hexToRgbTriplet(theme.colors.muted),
    "--color-border": hexToRgbTriplet(theme.colors.border),
    "--color-accent": hexToRgbTriplet(theme.colors.accent),
    "--color-accent-hover": hexToRgbTriplet(theme.colors.accentHover),
    "--color-error": hexToRgbTriplet(theme.colors.error),
    "--font-sans": theme.fonts.sans,
    "--font-display": theme.fonts.display,
    "--font-mono": theme.fonts.mono,
    "--display-transform": theme.display.transform,
    "--display-tracking": theme.display.tracking,
    "--display-style": theme.display.style,
    "--radius": theme.geometry.radius,
    "--node-gap": theme.geometry.nodeGap,
    "--scroll-headroom": theme.geometry.scrollHeadroom,
    "--h1-size": theme.headers.h1.size,
    "--h1-weight": String(theme.headers.h1.weight),
    "--h1-color": hexToRgbTriplet(theme.headers.h1.color),
    "--h2-size": theme.headers.h2.size,
    "--h2-weight": String(theme.headers.h2.weight),
    "--h2-color": hexToRgbTriplet(theme.headers.h2.color),
    "--h3-size": theme.headers.h3.size,
    "--h3-weight": String(theme.headers.h3.weight),
    "--h3-color": hexToRgbTriplet(theme.headers.h3.color),
    "--h4-size": theme.headers.h4.size,
    "--h4-weight": String(theme.headers.h4.weight),
    "--h4-color": hexToRgbTriplet(theme.headers.h4.color),
    "--bold-color": hexToRgbTriplet(theme.emphasis.bold.color),
    "--bold-weight": String(theme.emphasis.bold.weight),
    "--italic-color": hexToRgbTriplet(theme.emphasis.italic.color),
    "--underline-color": hexToRgbTriplet(theme.emphasis.underline.color),
    "--form-title-color": hexToRgbTriplet(theme.formTitle.color),
  };
}

/**
 * Ensure a Google Fonts <link> is present for the given href. Reuses a
 * single managed <link> per id so repeated calls don't accumulate tags.
 */
export function ensureFontLink(
  href: string | null,
  id = "theme-fonts",
): void {
  if (!href) return;
  let link = document.getElementById(id) as HTMLLinkElement | null;
  if (!link) {
    link = document.createElement("link");
    link.id = id;
    link.rel = "stylesheet";
    document.head.appendChild(link);
  }
  if (link.href !== href) link.href = href;
}

/**
 * Apply the theme to the document at runtime. Writes CSS variables to
 * <html> and injects the Google Fonts <link> if specified.
 *
 * Called once on app boot from main.tsx for the product theme. Per-form
 * themes use `themeToCssVars` on a scoping element instead.
 */
export function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  for (const [key, value] of Object.entries(themeToCssVars(theme))) {
    root.style.setProperty(key, value);
  }
  root.dataset.grain = theme.effects.grain ? "on" : "off";
  ensureFontLink(theme.fonts.googleFontsHref);
}

/**
 * Convert "#RRGGBB" to "R G B" (space-separated decimal triplet), the
 * format Tailwind expects for CSS-variable colors with alpha support.
 * Tolerant — an invalid value yields "0 0 0" rather than throwing, so a
 * half-typed hex in the theme editor can't crash the live preview.
 */
function hexToRgbTriplet(hex: string): string {
  const normalized = hex.replace("#", "").trim();
  if (!/^[0-9a-fA-F]{6}$/.test(normalized)) return "0 0 0";
  const r = parseInt(normalized.slice(0, 2), 16);
  const g = parseInt(normalized.slice(2, 4), 16);
  const b = parseInt(normalized.slice(4, 6), 16);
  return `${r} ${g} ${b}`;
}
