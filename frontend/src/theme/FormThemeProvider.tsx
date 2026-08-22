import { useEffect } from "react";
import type { CSSProperties, ReactNode } from "react";
import { Outlet, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { getFormTheme, setUnlistedKey } from "../lib/api";
import { theme as defaultTheme } from "./theme";
import { themeToCssVars, ensureFontLink } from "./applyTheme";

/**
 * Scopes the per-form theme over the live-form routes.
 *
 * The product theme lives on <html> (set once at boot). This wrapper
 * sets the same CSS variables on a scoping element, so everything
 * inside — the form-facing views — resolves the theme-mapped Tailwind
 * classes (`bg-bg`, `text-ink`, …) to the form's own tokens instead.
 * A form without a custom theme falls back to the product default, so
 * there's no flash before the theme query resolves.
 *
 * It also captures the unlisted-link token (`?key=`) from the URL and
 * registers it with the API client, so an unlisted form opened via
 * its share link authorizes correctly. Done during render — before
 * the child fill pages fire their (visibility-gated) queries.
 */
export function FormThemeProvider({
  formId: formIdProp,
  children,
}: {
  /** Supplied when used as a wrapper rather than a layout route — a
   *  workspace panel knows its form but has no route to read it from. */
  formId?: string;
  children?: ReactNode;
} = {}) {
  const params = useParams();
  const [searchParams] = useSearchParams();
  const asWrapper = formIdProp !== undefined;
  const formId = formIdProp ?? params.formId;

  // Register the unlisted key synchronously, before <Outlet/> children
  // render and issue their API calls.
  //
  // Skipped in wrapper mode: the key is a module-level global, and a
  // workspace panel reading the *workspace* URL would clear a key that
  // belongs to a form opened elsewhere.
  if (!asWrapper) {
    setUnlistedKey(searchParams.get("key"));
  }

  const { data } = useQuery({
    queryKey: ["formTheme", formId],
    queryFn: () => getFormTheme(formId!),
    enabled: Boolean(formId),
    staleTime: 60_000,
  });

  const active = data ?? defaultTheme;

  useEffect(() => {
    ensureFontLink(active.fonts.googleFontsHref, "form-theme-fonts");
  }, [active.fonts.googleFontsHref]);

  return (
    <div
      style={themeToCssVars(active) as CSSProperties}
      data-grain={active.effects.grain ? "on" : "off"}
      className={asWrapper ? "min-h-full bg-bg" : "min-h-screen bg-bg"}
    >
      {children ?? <Outlet />}
    </div>
  );
}
