import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  getFormTheme,
  saveFormTheme,
  clearFormTheme,
} from "../lib/api";
import { type Theme } from "../theme/theme";
import { theme as defaultTheme } from "../theme/theme";
import { themeToCssVars, ensureFontLink } from "../theme/applyTheme";
import {
  FONT_PRESETS,
  matchFontPreset,
} from "../theme/fontPresets";

const COLOR_TOKENS: { key: keyof Theme["colors"]; label: string }[] = [
  { key: "bg", label: "Background" },
  { key: "surface", label: "Surface" },
  { key: "ink", label: "Text" },
  { key: "muted", label: "Muted text" },
  { key: "border", label: "Border" },
  { key: "accent", label: "Accent" },
  { key: "accentHover", label: "Accent hover" },
  { key: "error", label: "Error" },
];

const RADIUS_OPTIONS = ["0", "2px", "4px", "8px", "12px", "16px"];

const HEADER_LEVELS: { key: keyof Theme["headers"]; label: string }[] = [
  { key: "h1", label: "H1" },
  { key: "h2", label: "H2" },
  { key: "h3", label: "H3" },
  { key: "h4", label: "H4" },
];

const WEIGHT_OPTIONS = [400, 500, 600, 700, 800];

/**
 * Theme editor (`/forms/:formId/theme`) — edits the form's per-form
 * theme: all eight colors, a font preset, corner radius, and display
 * typography. The right-hand panel previews the form-facing styling
 * live; Save persists, Revert clears the customization.
 */
export default function FormThemePage() {
  const { formId } = useParams<{ formId: string }>();
  const queryClient = useQueryClient();

  const { data, isLoading, error } = useQuery({
    queryKey: ["formTheme", formId],
    queryFn: () => getFormTheme(formId!),
    enabled: Boolean(formId),
  });

  const [draft, setDraft] = useState<Theme>(defaultTheme);
  const [loaded, setLoaded] = useState(false);

  // Seed the editor once the saved theme (or its absence) resolves.
  useEffect(() => {
    if (!loaded && !isLoading) {
      setDraft(data ?? defaultTheme);
      setLoaded(true);
    }
  }, [data, isLoading, loaded]);

  // Keep the preview's web font available as the preset changes.
  useEffect(() => {
    ensureFontLink(draft.fonts.googleFontsHref, "theme-editor-fonts");
  }, [draft.fonts.googleFontsHref]);

  const save = useMutation({
    mutationFn: () => saveFormTheme(formId!, draft),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["formTheme", formId] });
    },
  });

  const revert = useMutation({
    mutationFn: () => clearFormTheme(formId!),
    onSuccess: () => {
      setDraft(defaultTheme);
      queryClient.invalidateQueries({ queryKey: ["formTheme", formId] });
    },
  });

  function setColor(key: keyof Theme["colors"], value: string) {
    setDraft((d) => ({ ...d, colors: { ...d.colors, [key]: value } }));
  }
  function setFontPreset(id: string) {
    const preset = FONT_PRESETS.find((p) => p.id === id);
    if (preset) setDraft((d) => ({ ...d, fonts: preset.fonts }));
  }
  function setRadius(value: string) {
    setDraft((d) => ({ ...d, geometry: { ...d.geometry, radius: value } }));
  }
  function setDisplay<K extends keyof Theme["display"]>(
    key: K,
    value: Theme["display"][K],
  ) {
    setDraft((d) => ({ ...d, display: { ...d.display, [key]: value } }));
  }
  function setHeader(
    level: keyof Theme["headers"],
    field: "size" | "weight" | "color",
    value: string | number,
  ) {
    setDraft((d) => ({
      ...d,
      headers: {
        ...d.headers,
        [level]: { ...d.headers[level], [field]: value },
      },
    }));
  }
  function setEmphasis(
    kind: keyof Theme["emphasis"],
    field: string,
    value: string | number,
  ) {
    setDraft((d) => ({
      ...d,
      emphasis: {
        ...d.emphasis,
        [kind]: { ...d.emphasis[kind], [field]: value },
      },
    }));
  }
  function setFormTitleColor(value: string) {
    setDraft((d) => ({ ...d, formTitle: { color: value } }));
  }

  const presetId = matchFontPreset(draft.fonts) ?? "";

  return (
    <main className="relative z-10 mx-auto max-w-7xl px-6 pt-24 pb-16">
      <header className="mb-10">
        <Link
          to={`/forms/${encodeURIComponent(formId ?? "")}`}
          className="font-mono text-xs uppercase tracking-wider text-muted hover:text-accent"
        >
          ← Back to form
        </Link>
        <h1 className="mt-4 font-display text-4xl font-bold leading-tight text-ink">
          Theme
        </h1>
        <p className="mt-3 max-w-xl font-sans text-sm leading-relaxed text-muted">
          Styling for this form's end-user views. The builder console
          keeps its own theme.
        </p>
      </header>

      {error ? (
        <div className="border border-error bg-surface p-6">
          <p className="text-sm text-error">
            Couldn't load the theme:{" "}
            {error instanceof ApiError ? error.message : "unknown error"}
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-10 lg:grid-cols-[1fr_minmax(360px,440px)]">
          <ThemeControls
            draft={draft}
            presetId={presetId}
            onColor={setColor}
            onFontPreset={setFontPreset}
            onRadius={setRadius}
            onDisplay={setDisplay}
            onHeader={setHeader}
            onEmphasis={setEmphasis}
            onFormTitle={setFormTitleColor}
          />
          <div className="lg:sticky lg:top-24 lg:self-start">
            <ThemePreview theme={draft} />
            <div className="mt-4 flex flex-wrap items-center gap-3">
              <button
                type="button"
                onClick={() => save.mutate()}
                disabled={save.isPending || !loaded}
                className="bg-accent px-4 py-2 font-mono text-xs uppercase tracking-wider text-bg hover:bg-accent-hover disabled:opacity-40"
              >
                {save.isPending ? "Saving…" : "Save theme"}
              </button>
              <button
                type="button"
                onClick={() => revert.mutate()}
                disabled={revert.isPending}
                className="border border-border px-4 py-2 font-mono text-xs uppercase tracking-wider text-muted hover:text-ink disabled:opacity-40"
              >
                Revert to default
              </button>
              {save.isSuccess && !save.isPending ? (
                <span className="font-mono text-xs uppercase tracking-wider text-muted">
                  Saved
                </span>
              ) : null}
              {save.error ? (
                <span className="font-mono text-xs uppercase tracking-wider text-error">
                  Save failed
                </span>
              ) : null}
            </div>
          </div>
        </div>
      )}
    </main>
  );
}

// --- Controls --------------------------------------------------------------

interface ControlsProps {
  draft: Theme;
  presetId: string;
  onColor: (k: keyof Theme["colors"], v: string) => void;
  onFontPreset: (id: string) => void;
  onRadius: (v: string) => void;
  onDisplay: <K extends keyof Theme["display"]>(
    k: K,
    v: Theme["display"][K],
  ) => void;
  onHeader: (
    level: keyof Theme["headers"],
    field: "size" | "weight" | "color",
    value: string | number,
  ) => void;
  onEmphasis: (
    kind: keyof Theme["emphasis"],
    field: string,
    value: string | number,
  ) => void;
  onFormTitle: (value: string) => void;
}

function ThemeControls({
  draft,
  presetId,
  onColor,
  onFontPreset,
  onRadius,
  onDisplay,
  onHeader,
  onEmphasis,
  onFormTitle,
}: ControlsProps) {
  return (
    <div className="flex flex-col gap-8">
      <Section title="Colors">
        <div className="grid grid-cols-1 gap-x-8 gap-y-3 sm:grid-cols-2">
          {COLOR_TOKENS.map((t) => (
            <ColorRow
              key={t.key}
              label={t.label}
              value={draft.colors[t.key]}
              onChange={(v) => onColor(t.key, v)}
            />
          ))}
        </div>
      </Section>

      <Section title="Headers">
        <p className="mb-3 font-sans text-xs text-muted">
          Heading levels in form content (markdown #–####).
        </p>
        <div className="flex flex-col gap-2.5">
          {HEADER_LEVELS.map((h) => (
            <div key={h.key} className="flex items-center gap-3">
              <span className="w-7 shrink-0 font-mono text-xs uppercase text-muted">
                {h.label}
              </span>
              <input
                type="text"
                value={draft.headers[h.key].size}
                onChange={(e) => onHeader(h.key, "size", e.target.value)}
                spellCheck={false}
                aria-label={`${h.label} size`}
                className="w-24 border border-border bg-bg px-2 py-1.5 font-mono text-xs text-ink"
              />
              <select
                value={draft.headers[h.key].weight}
                onChange={(e) =>
                  onHeader(h.key, "weight", Number(e.target.value))
                }
                aria-label={`${h.label} weight`}
                className="border border-border bg-bg px-2 py-1.5 font-sans text-sm text-ink"
              >
                {WEIGHT_OPTIONS.map((w) => (
                  <option key={w} value={w}>
                    {w}
                  </option>
                ))}
              </select>
              <Swatch
                value={draft.headers[h.key].color}
                onChange={(v) => onHeader(h.key, "color", v)}
                label={`${h.label} color`}
              />
            </div>
          ))}
        </div>
      </Section>

      <Section title="Emphasis">
        <p className="mb-3 font-sans text-xs text-muted">
          Inline styles in form content — bold, italic, underline.
        </p>
        <div className="flex flex-col gap-2.5">
          <div className="flex items-center gap-3">
            <span className="w-20 shrink-0 font-sans text-sm text-ink">
              Bold
            </span>
            <select
              value={draft.emphasis.bold.weight}
              onChange={(e) =>
                onEmphasis("bold", "weight", Number(e.target.value))
              }
              aria-label="Bold weight"
              className="border border-border bg-bg px-2 py-1.5 font-sans text-sm text-ink"
            >
              {WEIGHT_OPTIONS.map((w) => (
                <option key={w} value={w}>
                  {w}
                </option>
              ))}
            </select>
            <Swatch
              value={draft.emphasis.bold.color}
              onChange={(v) => onEmphasis("bold", "color", v)}
              label="Bold color"
            />
          </div>
          <div className="flex items-center gap-3">
            <span className="w-20 shrink-0 font-sans text-sm text-ink">
              Italic
            </span>
            <Swatch
              value={draft.emphasis.italic.color}
              onChange={(v) => onEmphasis("italic", "color", v)}
              label="Italic color"
            />
          </div>
          <div className="flex items-center gap-3">
            <span className="w-20 shrink-0 font-sans text-sm text-ink">
              Underline
            </span>
            <Swatch
              value={draft.emphasis.underline.color}
              onChange={(v) => onEmphasis("underline", "color", v)}
              label="Underline color"
            />
          </div>
        </div>
      </Section>

      <Section title="Form title">
        <div className="flex items-center gap-3">
          <span className="w-20 shrink-0 font-sans text-sm text-ink">
            Title color
          </span>
          <Swatch
            value={draft.formTitle.color}
            onChange={onFormTitle}
            label="Form title color"
          />
        </div>
      </Section>

      <Section title="Typeface">
        <Field label="Font">
          <select
            value={presetId}
            onChange={(e) => onFontPreset(e.target.value)}
            className="w-full border border-border bg-bg px-2 py-1.5 font-sans text-sm text-ink"
          >
            {presetId === "" ? (
              <option value="">Custom</option>
            ) : null}
            {FONT_PRESETS.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>
        </Field>
      </Section>

      <Section title="Geometry">
        <Field label="Corner radius">
          <select
            value={
              RADIUS_OPTIONS.includes(draft.geometry.radius)
                ? draft.geometry.radius
                : RADIUS_OPTIONS[0]
            }
            onChange={(e) => onRadius(e.target.value)}
            className="w-full border border-border bg-bg px-2 py-1.5 font-sans text-sm text-ink"
          >
            {RADIUS_OPTIONS.map((r) => (
              <option key={r} value={r}>
                {r === "0" ? "0 — sharp" : r}
              </option>
            ))}
          </select>
        </Field>
      </Section>

      <Section title="Display type">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="Case">
            <select
              value={draft.display.transform}
              onChange={(e) =>
                onDisplay(
                  "transform",
                  e.target.value as Theme["display"]["transform"],
                )
              }
              className="w-full border border-border bg-bg px-2 py-1.5 font-sans text-sm text-ink"
            >
              <option value="none">Normal case</option>
              <option value="uppercase">Uppercase</option>
              <option value="lowercase">Lowercase</option>
            </select>
          </Field>
          <Field label="Style">
            <select
              value={draft.display.style}
              onChange={(e) =>
                onDisplay(
                  "style",
                  e.target.value as Theme["display"]["style"],
                )
              }
              className="w-full border border-border bg-bg px-2 py-1.5 font-sans text-sm text-ink"
            >
              <option value="normal">Regular</option>
              <option value="italic">Italic</option>
            </select>
          </Field>
        </div>
      </Section>
    </div>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <h2 className="mb-3 font-mono text-xs uppercase tracking-[0.15em] text-muted">
        {title}
      </h2>
      {children}
    </section>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="font-mono text-[10px] uppercase tracking-wider text-muted">
        {label}
      </span>
      {children}
    </label>
  );
}

function ColorRow({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
}) {
  const safe = /^#[0-9a-fA-F]{6}$/.test(value) ? value : "#000000";
  return (
    <div className="flex items-center gap-3">
      <input
        type="color"
        value={safe}
        onChange={(e) => onChange(e.target.value.toUpperCase())}
        className="h-8 w-10 shrink-0 cursor-pointer border border-border bg-bg p-0.5"
        aria-label={label}
      />
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="truncate font-sans text-sm text-ink">
          {label}
        </span>
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value.toUpperCase())}
          spellCheck={false}
          className="w-24 border-0 bg-transparent p-0 font-mono text-xs text-muted focus:text-ink focus:outline-none"
        />
      </div>
    </div>
  );
}

/** A compact color swatch — the picker alone, for inline rows. */
function Swatch({
  value,
  onChange,
  label,
}: {
  value: string;
  onChange: (v: string) => void;
  label: string;
}) {
  const safe = /^#[0-9a-fA-F]{6}$/.test(value) ? value : "#000000";
  return (
    <input
      type="color"
      value={safe}
      onChange={(e) => onChange(e.target.value.toUpperCase())}
      className="h-8 w-10 shrink-0 cursor-pointer border border-border bg-bg p-0.5"
      aria-label={label}
    />
  );
}

// --- Preview ---------------------------------------------------------------

/** A representative slice of form-facing UI, rendered with the draft
 *  theme's tokens so edits are visible immediately. */
function ThemePreview({ theme }: { theme: Theme }) {
  const vars = themeToCssVars(theme) as CSSProperties;
  return (
    <div>
      <h2 className="mb-3 font-mono text-xs uppercase tracking-[0.15em] text-muted">
        Preview
      </h2>
      <div
        style={vars}
        className="border border-border bg-bg p-6"
      >
        <p
          className="font-display font-bold"
          style={{
            fontSize: "1.75rem",
            color: "rgb(var(--form-title-color))",
            textTransform: theme.display.transform,
            letterSpacing: theme.display.tracking,
            fontStyle: theme.display.style,
          }}
        >
          Publish an article
        </p>
        <p className="mt-1 font-mono text-[10px] uppercase tracking-wider text-muted">
          ↑ form title
        </p>

        <div className="mt-5 flex flex-col gap-1.5">
          {HEADER_LEVELS.map((h) => (
            <span
              key={h.key}
              className="font-display"
              style={{
                fontSize: theme.headers[h.key].size,
                fontWeight: theme.headers[h.key].weight,
                color: `rgb(var(--${h.key}-color))`,
                textTransform: theme.display.transform,
                letterSpacing: theme.display.tracking,
                fontStyle: theme.display.style,
              }}
            >
              {h.label} heading sample
            </span>
          ))}
        </div>

        <p className="mt-4 font-sans text-sm leading-relaxed text-muted">
          Body text with{" "}
          <strong
            style={{
              fontWeight: theme.emphasis.bold.weight,
              color: theme.emphasis.bold.color,
            }}
          >
            bold
          </strong>
          ,{" "}
          <em style={{ color: theme.emphasis.italic.color }}>italic</em>,
          and{" "}
          <u style={{ color: theme.emphasis.underline.color }}>
            underlined
          </u>{" "}
          words.
        </p>

        <div className="mt-5 flex flex-col gap-1.5">
          <span className="font-mono text-[10px] uppercase tracking-wider text-muted">
            Full name
          </span>
          <input
            type="text"
            readOnly
            value="Jordan Avery"
            className="border border-border bg-surface px-3 py-2 font-sans text-sm text-ink"
            style={{ borderRadius: theme.geometry.radius }}
          />
        </div>

        <div className="mt-3 flex flex-col gap-1.5">
          <span className="font-mono text-[10px] uppercase tracking-wider text-muted">
            Email
          </span>
          <input
            type="text"
            readOnly
            value="not a valid address"
            className="border border-error bg-surface px-3 py-2 font-sans text-sm text-ink"
            style={{ borderRadius: theme.geometry.radius }}
          />
          <span className="font-sans text-xs text-error">
            Enter a valid email address.
          </span>
        </div>

        <div className="mt-5 flex items-center gap-3">
          <button
            type="button"
            className="bg-accent px-4 py-2 font-mono text-xs uppercase tracking-wider text-bg"
            style={{ borderRadius: theme.geometry.radius }}
          >
            Submit
          </button>
          <button
            type="button"
            className="border border-border px-4 py-2 font-mono text-xs uppercase tracking-wider text-muted"
            style={{ borderRadius: theme.geometry.radius }}
          >
            Back
          </button>
        </div>

        <div
          className="mt-5 border border-border bg-surface p-3"
          style={{ borderRadius: theme.geometry.radius }}
        >
          <span className="font-mono text-[10px] uppercase tracking-wider text-muted">
            Surface panel
          </span>
        </div>
      </div>
    </div>
  );
}
