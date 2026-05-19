import type { Theme } from "./theme";

/**
 * Curated font presets for the theme editor. Each sets all three font
 * slots and the Google Fonts URL together — picking a typeface is one
 * coherent choice, not three independent stacks.
 */
export interface FontPreset {
  id: string;
  label: string;
  fonts: Theme["fonts"];
}

export const FONT_PRESETS: FontPreset[] = [
  {
    id: "inter",
    label: "Inter — modern sans",
    fonts: {
      sans: '"Inter", system-ui, -apple-system, sans-serif',
      display: '"Inter", system-ui, -apple-system, sans-serif',
      mono: '"JetBrains Mono", ui-monospace, "SF Mono", monospace',
      googleFontsHref:
        "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap",
    },
  },
  {
    id: "serif",
    label: "Lora — editorial serif",
    fonts: {
      sans: '"Inter", system-ui, sans-serif',
      display: '"Lora", Georgia, serif',
      mono: '"JetBrains Mono", ui-monospace, monospace',
      googleFontsHref:
        "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Lora:wght@500;600;700&family=JetBrains+Mono:wght@400;500&display=swap",
    },
  },
  {
    id: "geometric",
    label: "Poppins — geometric",
    fonts: {
      sans: '"Poppins", system-ui, sans-serif',
      display: '"Poppins", system-ui, sans-serif',
      mono: '"JetBrains Mono", ui-monospace, monospace',
      googleFontsHref:
        "https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap",
    },
  },
  {
    id: "system",
    label: "System — no web fonts",
    fonts: {
      sans: 'system-ui, -apple-system, "Segoe UI", sans-serif',
      display: 'system-ui, -apple-system, "Segoe UI", sans-serif',
      mono: 'ui-monospace, "SF Mono", "Cascadia Mono", monospace',
      googleFontsHref: null,
    },
  },
];

/** The preset whose fonts match the given set, or null when custom. */
export function matchFontPreset(fonts: Theme["fonts"]): string | null {
  const hit = FONT_PRESETS.find(
    (p) =>
      p.fonts.sans === fonts.sans &&
      p.fonts.display === fonts.display &&
      p.fonts.mono === fonts.mono,
  );
  return hit ? hit.id : null;
}
