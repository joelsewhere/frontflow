/**
 * An embedded Superset dashboard, rendered inside a form's layout tree.
 *
 * The compiled block carries only the dashboard NAME. Everything else —
 * the Superset origin, the embed UUID, the refresh filter — is resolved
 * at render time from `/api/forms/{formId}/dashboards/{name}/embed`,
 * because a form's compiled tree is snapshotted per form_version and a
 * baked-in UUID would pin the form to whichever dashboard existed the
 * day it was written.
 *
 * Nothing here refreshes the dashboard. That is `RefreshDashboard`, an
 * operator the author places in the execution chain, so a refresh
 * happens where they said it should.
 *
 * Lives in its own file rather than inside BlockTree.tsx: it owns an
 * iframe lifecycle and an SDK dependency, and BlockTree is long enough.
 */

import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  getDashboardEmbedConfig,
  getDashboardGuestToken,
  getWorkspaceDashboardEmbedConfig,
  getWorkspaceDashboardGuestToken,
} from "../../lib/api";
import { useSubmission } from "../../hooks/useSubmission";
import { embedDashboard } from "../../vendor/superset-embedded-sdk";

type EmbeddedDashboard = Awaited<ReturnType<typeof embedDashboard>>;

export interface DashboardBlockProps {
  /** Set when the dashboard sits inside a form; the form's ACL
   *  authorizes it. Mutually exclusive with workspaceId. */
  formId: string | null;
  submissionId: string | null;
  /** Set when the dashboard is a workspace panel; the workspace's own
   *  visibility authorizes it, since there is no form to inherit from. */
  workspaceId?: string | null;
  name: string;
  height: number;
  showFilters: boolean;
  /** Fill the parent instead of using `height` — a dock panel sizes
   *  itself, whereas a form layout scrolls and needs a fixed height. */
  fill?: boolean;
  /** Offer the Superset editor for this dashboard. Set from the
   *  workspace's `can_edit_dashboards`. Only ever affects the DASHBOARD:
   *  a form's definition lives in its DSL source and is never editable
   *  from the UI. */
  canEdit?: boolean;
}

export function DashboardEmbed({
  formId,
  submissionId,
  workspaceId = null,
  name,
  height,
  showFilters,
  fill = false,
  canEdit = false,
}: DashboardBlockProps) {
  const [mode, setMode] = useState<"view" | "edit" | "new">("view");
  // Exactly one scope authorizes the embed.
  const scoped = Boolean(formId) || Boolean(workspaceId);
  const mountRef = useRef<HTMLDivElement | null>(null);
  const dashboardRef = useRef<EmbeddedDashboard | null>(null);
  const [embedError, setEmbedError] = useState<string | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  // Tokens already applied. A poll returns the same chain state
  // repeatedly, so without this the dashboard would re-query on every
  // tick rather than once per requested refresh.
  const handledTokens = useRef<Set<string>>(new Set());

  const config = useQuery({
    queryKey: ["dashboard-embed", workspaceId, formId, name],
    queryFn: () =>
      workspaceId
        ? getWorkspaceDashboardEmbedConfig(workspaceId, name)
        : getDashboardEmbedConfig(formId as string, name),
    enabled: scoped,
    // Provisioning can take a moment on first use; one retry covers a
    // slow Superset without hammering a down one.
    retry: 1,
  });

  const embedUuid = config.data?.embed_uuid ?? null;
  const supersetDomain = config.data?.superset_domain ?? null;
  const filterId = config.data?.filter_id ?? null;

  useEffect(() => {
    if (!scoped || !embedUuid || !supersetDomain) return;
    const element = mountRef.current;
    if (!element) return;

    let cancelled = false;
    setEmbedError(null);

    embedDashboard({
      id: embedUuid,
      supersetDomain,
      mountPoint: element,
      fetchGuestToken: () =>
        workspaceId
          ? getWorkspaceDashboardGuestToken(workspaceId, name)
          : getDashboardGuestToken(formId as string, name),
      dashboardUiConfig: {
        hideTitle: true,
        hideChartControls: false,
        filters: { expanded: false, visible: showFilters },
      },
    })
      .then((dashboard) => {
        // StrictMode double-mounts in development; without this guard
        // the second mount leaks the first iframe.
        if (cancelled) {
          dashboard.unmount();
          return;
        }
        dashboardRef.current = dashboard;
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setEmbedError(err instanceof Error ? err.message : String(err));
      });

    return () => {
      cancelled = true;
      dashboardRef.current?.unmount();
      dashboardRef.current = null;
    };
  }, [scoped, formId, workspaceId, name, embedUuid, supersetDomain, showFilters]);

  // Refresh directives ride the submission the page is already polling.
  // Same query key as SubmissionPage, so react-query serves this from
  // cache — no additional request and no second polling loop.
  const submission = useSubmission(
    formId ?? undefined,
    submissionId ?? undefined,
  );

  const pending = latestRefreshFor(submission.data?.tasks, name);

  useEffect(() => {
    const dashboard = dashboardRef.current;
    if (!dashboard || !pending) return;
    if (handledTokens.current.has(pending.token)) return;
    if (!filterId) return;

    handledTokens.current.add(pending.token);
    setIsRefreshing(true);

    // Moving the filter re-queries the charts inside the existing
    // iframe. The frame is never remounted — a remount is the visible
    // flash this whole design exists to avoid.
    dashboard
      .setDataMask({
        [filterId]: {
          extraFormData: { time_range: pending.time_range },
          filterState: { value: pending.time_range, label: "Latest" },
        },
      })
      .catch((err: unknown) => {
        setEmbedError(
          `Refresh failed: ${err instanceof Error ? err.message : String(err)}`,
        );
      })
      .finally(() => setIsRefreshing(false));
  }, [pending, filterId]);

  if (!scoped) {
    // No form context — a preview surface that renders blocks outside a
    // form. Nothing to authorize against, so nothing to embed.
    return (
      <Placeholder height={height}>
        Dashboard <Code>{name}</Code> renders when the form is opened.
      </Placeholder>
    );
  }

  if (config.isPending) {
    return <Placeholder height={height}>Loading dashboard…</Placeholder>;
  }

  if (config.isError) {
    return (
      <Notice height={height} tone="error">
        <strong>Dashboard unavailable.</strong>
        <p className="mt-1">{(config.error as Error).message}</p>
      </Notice>
    );
  }

  if (!embedUuid) {
    return (
      <Notice height={height} tone="warn">
        <strong>
          Dashboard <Code>{name}</Code> is not embeddable yet.
        </strong>
        <p className="mt-1">
          It has no embed configuration — an administrator can repair it
          from the dashboards admin API.
        </p>
      </Notice>
    );
  }

  const supersetDashboardId = config.data?.superset_dashboard_id ?? null;
  // Editor and new-chart run under the viewer's Superset session, so they
  // must use the session hostname — not the embed one, whose cookie jar
  // is deliberately separate.
  const sessionDomain =
    config.data?.superset_session_domain ?? supersetDomain ?? "";
  const canOfferEdit = canEdit && Boolean(supersetDashboardId);

  // Superset surfaces we navigate to ourselves.
  //
  // Superset's own "create chart" links carry target=_blank, and the
  // frame is cross-origin, so the parent cannot intercept a click inside
  // it. What we CAN do is drive the frame's src ourselves — navigation
  // we initiate stays in the panel. Links Superset renders internally
  // will still occasionally break out to a tab; that is not fixable from
  // outside the frame.
  const frameUrl =
    mode === "edit" && supersetDashboardId
      ? `${sessionDomain}/superset/dashboard/${encodeURIComponent(supersetDashboardId)}/?standalone=${STANDALONE_HIDE_NAV}`
      : mode === "new"
        ? `${sessionDomain}/chart/add?standalone=${STANDALONE_HIDE_NAV}`
        : null;

  return (
    <div className={fill ? "flex h-full flex-col" : "w-full"}>
      {canOfferEdit && (
        <div className="mb-1 flex items-center justify-end">
          <div className="inline-flex overflow-hidden rounded border border-border text-xs">
            <ModeButton
              active={mode === "view"}
              onClick={() => setMode("view")}
              title="The live dashboard"
            >
              View
            </ModeButton>
            <ModeButton
              active={mode === "edit"}
              onClick={() => setMode("edit")}
              title="Edit this dashboard in Superset"
            >
              Edit
            </ModeButton>
            <ModeButton
              active={mode === "new"}
              onClick={() => setMode("new")}
              title="Build a new chart from a dataset"
            >
              New chart
            </ModeButton>
          </div>
        </div>
      )}

      {frameUrl ? (
        <SupersetFrame url={frameUrl} fill={fill} height={height} />
      ) : (
        <>
      {embedError && (
        <Notice height={null} tone="error">
          <strong>Could not load the dashboard.</strong>
          <p className="mt-1">{embedError}</p>
        </Notice>
      )}
      {!config.data?.filter_id && (
        <Notice height={null} tone="warn">
          This dashboard has no refresh filter, so it will not update in
          place. An administrator can repair it.
        </Notice>
      )}
      {isRefreshing && (
        <div
          className="mb-1 text-xs text-muted"
          role="status"
          aria-live="polite"
        >
          Updating…
        </div>
      )}
      <div
        ref={mountRef}
        data-dashboard={name}
        className={`ff-dashboard-embed w-full overflow-hidden rounded-md border border-border ${
          fill ? "min-h-0 flex-1" : ""
        }`}
        style={fill ? undefined : { height }}
      />
        </>
      )}
    </div>
  );
}

function ModeButton({
  active,
  onClick,
  title,
  children,
}: {
  active: boolean;
  onClick: () => void;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      className={`px-3 py-1 ${
        active
          ? "bg-accent font-semibold text-bg"
          : "text-muted hover:text-ink"
      }`}
    >
      {children}
    </button>
  );
}

/** DashboardStandaloneMode.HideNav — chrome-less but fully interactive. */
const STANDALONE_HIDE_NAV = 1;

/**
 * Superset's own UI in the panel, driven by the viewer's Superset
 * session rather than a guest token.
 *
 * A guest token cannot serve these surfaces: its user is anonymous, so
 * it can neither save a dashboard nor reach Explore (guest tokens grant
 * dashboards only). Anything beyond read-only viewing therefore needs
 * the person's own Superset login — which reaches a cross-site frame
 * only when Superset sets SameSite=None; Secure. Browsers that block it
 * regardless show a login screen here, hence the new-tab escape.
 */
function SupersetFrame({
  url,
  fill,
  height,
}: {
  url: string;
  fill: boolean;
  height: number;
}) {
  // Remounts the frame. Workspace panels render with dockview's
  // `defaultRenderer="always"`, so a hidden panel stays mounted rather
  // than being rebuilt on return — which is what preserves in-progress
  // work. The flip side is that a frame which loaded before you signed
  // in to Superset keeps showing that login page until something
  // reloads it, and switching tabs no longer does.
  //
  // Hence a manual control: automatic reloading would undo the very
  // persistence the renderer setting exists to provide.
  const [reloadKey, setReloadKey] = useState(0);

  return (
    <>
      <div className="mb-1 flex items-center gap-2 text-xs text-muted">
        <span>
          Superset, as your Superset user —{" "}
          <a
            href={url}
            target="_blank"
            rel="noreferrer"
            className="text-accent underline"
          >
            open in a new tab
          </a>{" "}
          if this frame shows a login screen.
        </span>
        <button
          type="button"
          onClick={() => setReloadKey((n) => n + 1)}
          className="ml-auto shrink-0 rounded border border-border px-2 py-0.5 hover:text-ink"
          title="Reload this frame — use it after signing in to Superset elsewhere"
        >
          Reload
        </button>
      </div>
      <iframe
        key={reloadKey}
        src={url}
        title="Superset"
        className={`w-full rounded-md border border-border ${
          fill ? "min-h-0 flex-1" : ""
        }`}
        style={fill ? undefined : { height }}
      />
    </>
  );
}

function Placeholder({
  height,
  children,
}: {
  height: number;
  children: React.ReactNode;
}) {
  return (
    <div
      className="flex w-full items-center justify-center rounded-md border border-border bg-surface text-sm text-muted"
      style={{ height }}
    >
      {children}
    </div>
  );
}

function Notice({
  height,
  tone,
  children,
}: {
  height: number | null;
  tone: "error" | "warn";
  children: React.ReactNode;
}) {
  const toneClass =
    tone === "error"
      ? "border-error/40 bg-error/10"
      : "border-border bg-surface";
  return (
    <div
      className={`w-full rounded-md border p-3 text-sm ${toneClass}`}
      style={height ? { minHeight: height } : undefined}
      role="alert"
    >
      {children}
    </div>
  );
}

function Code({ children }: { children: React.ReactNode }) {
  return (
    <code className="rounded bg-surface px-1 py-0.5 font-mono text-xs">
      {children}
    </code>
  );
}


/**
 * The most recent refresh requested for `name` in this submission.
 *
 * A chain can hold several RefreshDashboard operators for the same
 * dashboard; only the newest matters, and tokens are strictly
 * increasing, so the maximum is the newest.
 */
function latestRefreshFor(
  tasks: { dashboard_refresh?: { dashboard: string; time_range: string; token: string } | null }[] | undefined,
  name: string,
) {
  if (!tasks) return null;
  const matching = tasks
    .map((t) => t.dashboard_refresh)
    .filter(
      (d): d is { dashboard: string; time_range: string; token: string } =>
        Boolean(d) && d!.dashboard === name,
    );
  if (matching.length === 0) return null;
  return matching.reduce((a, b) => (a.token >= b.token ? a : b));
}
