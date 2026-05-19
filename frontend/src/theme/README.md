# Theme system

This directory contains everything that controls the application's visual
appearance. To re-skin the app for a different brand or environment, edit
the files here — no component code changes required.

## Files

| File | Purpose |
| --- | --- |
| `theme.ts` | The typed `Theme` interface and the active `theme` constant. **The single source of truth.** Edit this to change the application's appearance. |
| `applyTheme.ts` | Runtime: writes `theme` values into CSS variables on `<html>` and injects the Google Fonts `<link>`. Called once from `main.tsx`. |
| `theme.css` | Parse-time `:root` defaults that mirror `theme.ts`. Prevents a brief flash of unstyled content before JS executes. Also defines the `.font-display` utility class and animation keyframes. |
| `README.md` | This file. |

## How theming works

Three layers cooperate so changing one file changes the whole app:

```
theme.ts            ←  edit this
   │
   ├──> applyTheme.ts   writes CSS variables to <html>
   │                    ─ runs once at app boot
   │
   ├──> theme.css        also declares the same CSS variables on :root
   │                     ─ ensures correct render before JS runs
   │
   └──> tailwind.config.js   maps Tailwind classes (bg-accent, text-ink…)
                             onto those CSS variables
                             ─ build-time mapping
```

Components write Tailwind classes (`bg-surface`, `text-muted`, `font-display`)
that resolve to CSS variables that come from `theme.ts`. The pipeline is
one-way: components depend on Tailwind class names; Tailwind class names
resolve to CSS variables; CSS variables come from `theme.ts`.

## Changing the theme

### Just colors / fonts / sizing

Edit `theme.ts`. For example, to switch to a forest-green corporate theme:

```ts
export const theme: Theme = {
  name: "ForestCorp",
  fonts: { /* … */ },
  colors: {
    bg: "#FFFFFF",
    surface: "#F7F8F6",
    ink: "#0D1410",
    muted: "#5C6B62",
    border: "#DDE3DE",
    accent: "#1F5C3A",
    accentHover: "#163E27",
    error: "#A83232",
  },
  // …
};
```

Then **also update `theme.css`** to the same RGB triplets — it provides
the pre-JS default styling. (For `#1F5C3A`: R=31, G=92, B=58 → `--color-accent: 31 92 58`.)

### Editorial vs corporate aesthetic

The `display` object controls how display text renders:

| Style | `transform` | `tracking` | `style` |
| --- | --- | --- | --- |
| Corporate (current) | `"uppercase"` | `"-0.01em"` | `"normal"` |
| Editorial | `"none"` | `"normal"` | `"italic"` |
| Cinematic | `"uppercase"` | `"0.18em"` | `"normal"` |

Combined with a different `fonts.display`, this dramatically changes the
overall feel. The `font-display` class is applied to:

- Page hero headlines (`LandingPage`)
- `DagNode` titles
- Anything else that needs to read as "this is a heading"

### Geometry

| Token | Effect |
| --- | --- |
| `geometry.radius` | Corner radius on cards, inputs. `"0"` for sharp/corporate, `"12px"` for soft/friendly. |
| `geometry.nodeGap` | Vertical space between nodes in the DAG chain. |
| `geometry.scrollHeadroom` | Pixels of breathing room above newly-arrived nodes when auto-scrolling. |

### Effects

`effects.grain = true` enables a subtle paper-grain overlay (editorial
feel). Disabled for corporate themes.

## Adding a new token

If a future styling change can't be made by editing existing tokens, add
a new one:

1. Add to the `Theme` interface in `theme.ts`.
2. Set the value in the `theme` constant.
3. Write the variable in `applyTheme.ts`.
4. Add the default to `theme.css`.
5. Map it in `tailwind.config.js` if it's a color/font.
6. Use the new Tailwind class or CSS variable in components.

## Reference: the token contract

| CSS variable | Tailwind class | Source token |
| --- | --- | --- |
| `--color-bg` | `bg-bg`, `text-bg` | `colors.bg` |
| `--color-surface` | `bg-surface` | `colors.surface` |
| `--color-ink` | `text-ink`, `bg-ink` | `colors.ink` |
| `--color-muted` | `text-muted` | `colors.muted` |
| `--color-border` | `border-border` | `colors.border` |
| `--color-accent` | `bg-accent`, `text-accent` | `colors.accent` |
| `--color-accent-hover` | `bg-accent-hover`, `hover:bg-accent-hover` | `colors.accentHover` |
| `--color-error` | `text-error`, `border-error` | `colors.error` |
| `--font-sans` | `font-sans` | `fonts.sans` |
| `--font-display` | `font-display` (custom class) | `fonts.display` |
| `--font-mono` | `font-mono` | `fonts.mono` |
| `--radius` | (used directly in CSS where needed) | `geometry.radius` |

## Caveats

1. **Two sources for defaults.** `theme.ts` is the runtime source of truth,
   but `theme.css` carries duplicate defaults so the very first paint
   looks right. When you change `theme.ts`, also change `theme.css`. They
   are intentionally not auto-synced — keeping them as plain files makes
   the system easier to read and trivially portable.

2. **Component-level decisions remain in components.** The theme controls
   *what things look like*, not *what things appear*. Layout, hierarchy,
   spacing scale, copy, animation choreography — those stay in component
   files. A theme change can re-color and re-typeset; it doesn't reorganize.

3. **No runtime theme switching** is implemented in the UI yet. The
   architecture supports it cleanly — call `applyTheme(otherTheme)`
   and everything re-renders — but no toggle UI exists.
