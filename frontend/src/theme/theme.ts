/**
 * Workflow Runner — theme configuration.
 *
 * This file is the single source of truth for everything visual in the
 * application. To re-skin the app for a different brand or environment,
 * edit the `theme` constant at the bottom of this file.
 *
 * See src/theme/README.md for full documentation including the contract
 * between tokens and components.
 */

export interface Theme {
  /** Human-readable name. Surfaced in dev tools; otherwise informational. */
  name: string;

  fonts: {
    /** CSS font-family stack for body / UI text. */
    sans: string;
    /** CSS font-family stack for display (hero / heading) text. */
    display: string;
    /** CSS font-family stack for technical data, IDs, timestamps. */
    mono: string;
    /**
     * Google Fonts URL to load at runtime. Set to `null` if you're
     * self-hosting fonts or using system fonts only.
     */
    googleFontsHref: string | null;
  };

  /** Hex colors. Converted internally to RGB triplets for Tailwind alpha modifiers. */
  colors: {
    /** Page background. */
    bg: string;
    /** Card / panel background, slightly distinct from page bg. */
    surface: string;
    /** Primary text. */
    ink: string;
    /** Secondary text — labels, metadata. */
    muted: string;
    /** Borders, dividers. */
    border: string;
    /** Brand accent — CTAs, active states, brand-colored elements. */
    accent: string;
    /** Hover variant of accent. */
    accentHover: string;
    /** Errors, failed states, destructive actions. */
    error: string;
  };

  /**
   * Display typography behavior. Controls how text with the `.font-display`
   * class is rendered. Use this to switch between Editorial (mixed case,
   * italic, normal tracking) and Corporate (uppercase, tight tracking).
   */
  display: {
    /** Whether display text is forced uppercase. */
    transform: "none" | "uppercase" | "lowercase";
    /** Letter-spacing for display text — tighter for uppercase, normal otherwise. */
    tracking: string;
    /** Font style — "italic" reads as editorial, "normal" as corporate. */
    style: "normal" | "italic";
  };

  geometry: {
    /** Border radius applied to cards and inputs (e.g. "0", "4px", "12px"). */
    radius: string;
    /** Connector line length between DAG nodes (e.g. "2.5rem"). */
    nodeGap: string;
    /** Headroom when auto-scrolling to a new node (e.g. "80px"). */
    scrollHeadroom: string;
    /** Max width of @node form screens (any CSS length, e.g. "56rem"). */
    nodeWidth: string;
    /** Max width of @page screens and the outer canvas (e.g. "80rem"). */
    pageWidth: string;
  };

  /**
   * Heading levels for form content (the markdown headers `#`–`####`).
   * Each level sets a font-size, font-weight, and color; headings
   * render in the display font with the display typography treatment.
   */
  headers: {
    h1: HeaderStyle;
    h2: HeaderStyle;
    h3: HeaderStyle;
    h4: HeaderStyle;
  };

  /**
   * Inline emphasis in form content. Bold takes a color and a weight;
   * italic and underline take a color (italic is a slant, underline a
   * decoration — neither carries a weight of its own).
   */
  emphasis: {
    bold: { color: string; weight: number };
    italic: { color: string };
    underline: { color: string };
  };

  /** The form's title heading on the landing page. */
  formTitle: {
    color: string;
  };

  effects: {
    /** Decorative paper-grain overlay. Disable for a flat corporate look. */
    grain: boolean;
  };
}

/** One heading level's typographic treatment. */
export interface HeaderStyle {
  /** CSS font-size, e.g. "1.5rem" or "24px". */
  size: string;
  /** CSS font-weight, e.g. 600. */
  weight: number;
  /** Hex color. */
  color: string;
}

// ---------------------------------------------------------------------------
// Active theme
// ---------------------------------------------------------------------------
//
// The default theme: bold uppercase headlines, modern sans-serif
// (Inter), deep navy accent, clean neutral palette, no decorative grain.
//
// To re-skin: edit these values. To add a new theme, define another constant
// alongside this and import it from main.tsx instead.

export const theme: Theme = {
  name: "Default",

  fonts: {
    sans: '"Inter", system-ui, -apple-system, sans-serif',
    display: '"Inter", system-ui, -apple-system, sans-serif',
    mono: '"JetBrains Mono", ui-monospace, "SF Mono", monospace',
    googleFontsHref:
      "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap",
  },

  colors: {
    bg: "#FFFFFF",
    surface: "#FAFAFA",
    ink: "#0A0A0A",
    muted: "#6E6E6E",
    border: "#E5E5E5",
    accent: "#003C71",
    accentHover: "#002A54",
    error: "#C03030",
  },

  display: {
    transform: "uppercase",
    tracking: "-0.01em",
    style: "normal",
  },

  geometry: {
    radius: "0",
    nodeGap: "2.5rem",
    scrollHeadroom: "80px",
    nodeWidth: "56rem",
    pageWidth: "80rem",
  },

  headers: {
    h1: { size: "1.5rem", weight: 700, color: "#0A0A0A" },
    h2: { size: "1.25rem", weight: 600, color: "#0A0A0A" },
    h3: { size: "1rem", weight: 600, color: "#0A0A0A" },
    h4: { size: "0.875rem", weight: 600, color: "#0A0A0A" },
  },

  emphasis: {
    bold: { color: "#0A0A0A", weight: 700 },
    italic: { color: "#0A0A0A" },
    underline: { color: "#0A0A0A" },
  },

  formTitle: {
    color: "#0A0A0A",
  },

  effects: {
    grain: false,
  },
};
