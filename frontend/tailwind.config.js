/**
 * Tailwind config.
 *
 * All colors and fonts here resolve to CSS variables defined in
 * src/theme/theme.css and updated at runtime by src/theme/applyTheme.ts.
 * The active theme is the `theme` constant in src/theme/theme.ts.
 *
 * Don't add hardcoded colors or fonts to components — go through theme.ts.
 */

/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "rgb(var(--color-bg) / <alpha-value>)",
        surface: "rgb(var(--color-surface) / <alpha-value>)",
        ink: "rgb(var(--color-ink) / <alpha-value>)",
        muted: "rgb(var(--color-muted) / <alpha-value>)",
        border: "rgb(var(--color-border) / <alpha-value>)",
        accent: {
          DEFAULT: "rgb(var(--color-accent) / <alpha-value>)",
          hover: "rgb(var(--color-accent-hover) / <alpha-value>)",
        },
        error: "rgb(var(--color-error) / <alpha-value>)",
      },
      fontFamily: {
        sans: ["var(--font-sans)"],
        mono: ["var(--font-mono)"],
        // Note: font-display is intentionally NOT defined here. It's a
        // custom utility in theme.css that bundles font-family with
        // text-transform, letter-spacing, and font-style so the theme
        // can control all aspects of "display" typography together.
      },
      borderRadius: {
        // `rounded-theme` resolves to the theme's corner-radius token,
        // so per-form / product radius flows to inputs, buttons, cards.
        theme: "var(--radius)",
      },
    },
  },
  plugins: [],
};
