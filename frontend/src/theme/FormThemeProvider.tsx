import { useEffect } from "react";
import type { CSSProperties } from "react";
import { Outlet, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { getFormTheme, setUnlistedKey, setShareToken } from "../lib/api";
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
export function FormThemeProvider() {
  const { formId } = useParams();
  const [searchParams] = useSearchParams();

  // Register the unlisted key synchronously, before <Outlet/> children
  // render and issue their API calls.
  setUnlistedKey(searchParams.get("key"));
  // A read-only submission share link carries ?token=; register it so
  // the submission queries below authorize as its bearer. Only ever
  // SET it — an internal redirect can leave the token off the URL,
  // and clearing on absence would silently drop the credential
  // mid-session. Tokens are bound to one submission, so holding a
  // stale one grants nothing elsewhere.
  const shareToken = searchParams.get("token");
  if (shareToken) setShareToken(shareToken);

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
      className="min-h-screen bg-bg"
    >
      <Outlet />
    </div>
  );
}
