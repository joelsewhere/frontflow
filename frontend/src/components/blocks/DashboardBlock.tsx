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
}

export function DashboardEmbed({
  formId,
  submissionId,
  workspaceId = null,
  name,
  height,
  showFilters,
  fill = false,
}: DashboardBlockProps) {
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

  return (
    <div className={fill ? "flex h-full flex-col" : "w-full"}>
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
          fill ? "h-full" : ""
        }`}
        style={fill ? undefined : { height }}
      />
    </div>
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
