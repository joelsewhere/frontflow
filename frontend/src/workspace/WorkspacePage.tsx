/**
 * A workspace: several forms and dashboards docked on one screen.
 *
 * The arrangement is declared in the DSL — `displays.Row` / `Column`
 * around `workspace.Form(...)` and `displays.Dashboard(...)` — and this
 * page turns that tree into a dockview grid. The declaration is the
 * starting arrangement, not a straitjacket: from here panels can be
 * resized, re-docked, tabbed together, and collapsed.
 *
 * Layout changes are deliberately NOT persisted. The workspace's source
 * file is the single definition of what it looks like, the same way a
 * form's layout comes from its source; a per-user saved arrangement
 * would quietly diverge from it. Reset restores the declared layout.
 */

import { useCallback, useMemo, useRef } from "react";
import { DockviewReact } from "dockview";
import type {
  DockviewApi,
  DockviewReadyEvent,
  IDockviewPanelProps,
} from "dockview";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";

import { getWorkspace, type WorkspaceBlock } from "../lib/api";
import { CollapseProvider } from "./CollapseContext";
import { HeaderActions } from "./HeaderActions";
import { PanelTab } from "./PanelTab";
import { useCollapse } from "./useCollapse";
import {
  WorkspaceDashboardPanel,
  WorkspaceExplorePanel,
  WorkspaceFormPanel,
} from "./panels";

import "dockview/dist/styles/dockview.css";

interface PanelParams {
  workspaceId: string;
  /** Bumped by the header's Reload. Used as a React key so the panel's
   *  contents remount — the only way to refresh an iframe we cannot
   *  reach into, and the only way to refresh anything at all now that
   *  hidden panels stay mounted. */
  nonce?: number;
  formId?: string;
  name?: string;
  dataset?: string | null;
  showFilters?: boolean;
  canEdit?: boolean;
}

const components = {
  form: (props: IDockviewPanelProps<PanelParams>) => (
    <WorkspaceFormPanel
      key={props.params.nonce ?? 0}
      formId={props.params.formId as string}
    />
  ),
  dashboard: (props: IDockviewPanelProps<PanelParams>) => (
    <WorkspaceDashboardPanel
      key={props.params.nonce ?? 0}
      workspaceId={props.params.workspaceId}
      name={props.params.name as string}
      showFilters={Boolean(props.params.showFilters)}
      canEdit={Boolean(props.params.canEdit)}
    />
  ),
  explore: (props: IDockviewPanelProps<PanelParams>) => (
    <WorkspaceExplorePanel
      key={props.params.nonce ?? 0}
      workspaceId={props.params.workspaceId}
      dataset={props.params.dataset ?? null}
    />
  ),
};

const tabComponents = { default: PanelTab };

const PANEL_TYPES = new Set([
  "workspace_form",
  "dashboard",
  "superset_explore",
]);

/**
 * The declared tree, flattened into dock placements.
 *
 * Each entry carries the group it belongs to: panels under a `tabs`
 * container share one, so they open as tabs rather than splits. Every
 * other panel gets its own. This is only the starting arrangement —
 * dockview lets any tab be dragged out afterwards.
 */
interface Placement {
  block: WorkspaceBlock;
  /** Panels sharing a group key open as tabs in that group. */
  groupKey: string;
}

function collectPlacements(
  block: WorkspaceBlock,
  groupKey: string | null = null,
  out: Placement[] = [],
): Placement[] {
  if (PANEL_TYPES.has(block.type)) {
    out.push({
      block,
      // No enclosing `tabs` — the panel is its own group.
      groupKey: groupKey ?? `solo:${block.id ?? out.length}`,
    });
    return out;
  }

  const nextGroup =
    block.type === "tabs" ? (groupKey ?? `tabs:${block.id ?? out.length}`) : groupKey;

  for (const child of block.children ?? []) {
    collectPlacements(child, nextGroup, out);
  }
  return out;
}

/**
 * Whether the declared root splits horizontally.
 *
 * `displays.Row` means side by side, `Column` means stacked — the same
 * meaning those containers carry inside a form, so an author does not
 * learn a second vocabulary for workspaces.
 */
function rootIsRow(block: WorkspaceBlock): boolean {
  return block.type === "row";
}

function panelTitle(block: WorkspaceBlock): string {
  const props = block.props ?? {};
  if (block.type === "workspace_form") {
    return (props.title as string) ?? (props.form_id as string) ?? "Form";
  }
  if (block.type === "superset_explore") {
    return (
      (props.title as string) ??
      (props.dataset ? `Explore · ${props.dataset}` : "Explore")
    );
  }
  return (props.name as string) ?? "Dashboard";
}

export default function WorkspacePage() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const apiRef = useRef<DockviewApi | null>(null);

  const { toggle, isCollapsed } = useCollapse(containerRef);

  const workspace = useQuery({
    queryKey: ["workspace", workspaceId],
    queryFn: () => getWorkspace(workspaceId as string),
    enabled: Boolean(workspaceId),
  });

  const build = useCallback(
    (api: DockviewApi) => {
      if (!workspace.data || !workspaceId) return;
      api.clear();

      const placements = collectPlacements(workspace.data.layout);
      const horizontal = rootIsRow(workspace.data.layout);

      // First panel of each group starts a new split; later ones join it
      // as tabs.
      const groupAnchor = new Map<string, string>();
      let previousGroupKey: string | null = null;

      placements.forEach((placement, index) => {
        const { block, groupKey } = placement;
        const key = block.id ?? `${block.type}-${index}`;
        const anchor = groupAnchor.get(groupKey);

        api.addPanel({
          id: key,
          component:
            block.type === "workspace_form"
              ? "form"
              : block.type === "superset_explore"
                ? "explore"
                : "dashboard",
          title: panelTitle(block),
          params: {
            workspaceId,
            formId: block.props.form_id as string | undefined,
            name: block.props.name as string | undefined,
            dataset: (block.props.dataset as string | undefined) ?? null,
            showFilters: Boolean(block.props.show_filters),
            canEdit: workspace.data?.can_edit_dashboards ?? false,
          },
          ...(anchor
            ? // Same group — open as a tab beside its siblings.
              { position: { referencePanel: anchor, direction: "within" } }
            : previousGroupKey
              ? {
                  position: {
                    referencePanel: groupAnchor.get(previousGroupKey) as string,
                    direction: horizontal ? "right" : "below",
                  },
                }
              : {}),
        });

        if (!anchor) {
          groupAnchor.set(groupKey, key);
          previousGroupKey = groupKey;
        }
      });
    },
    [workspace.data, workspaceId],
  );

  const onReady = useCallback(
    (event: DockviewReadyEvent) => {
      apiRef.current = event.api;
      build(event.api);
    },
    [build],
  );

  // Rebuild when the declared layout changes (a source edit + rescan).
  const layoutSignature = JSON.stringify(workspace.data?.layout ?? null);
  useMemo(() => {
    if (apiRef.current) build(apiRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layoutSignature]);

  const resetLayout = useCallback(() => {
    if (apiRef.current) build(apiRef.current);
  }, [build]);

  const reloadPanel = useCallback((panelId: string) => {
    const panel = apiRef.current?.getPanel(panelId);
    if (!panel) return;
    // Changing a parameter re-renders the panel; the components use
    // `nonce` as their key, so the subtree — and any iframe in it —
    // is rebuilt from scratch.
    const current = (panel.params as { nonce?: number })?.nonce ?? 0;
    panel.api.updateParameters({ nonce: current + 1 });
  }, []);

  const collapseValue = useMemo(
    () => ({ toggle, isCollapsed, resetLayout, reloadPanel }),
    [toggle, isCollapsed, resetLayout, reloadPanel],
  );

  if (workspace.isPending) {
    return <div className="p-6 text-sm text-muted">Loading workspace…</div>;
  }

  if (workspace.isError) {
    return (
      <div className="m-6 rounded-md border border-error/40 bg-error/10 p-4 text-sm">
        <strong>Workspace unavailable.</strong>
        <p className="mt-1">{(workspace.error as Error).message}</p>
      </div>
    );
  }

  return (
    <CollapseProvider value={collapseValue}>
      <div className="flex h-screen flex-col bg-bg">
        <header className="flex flex-shrink-0 items-baseline gap-4 border-b border-border bg-surface px-4 py-2">
          <h1 className="text-[15px] font-semibold">{workspace.data.title}</h1>
          {workspace.data.description && (
            <p className="text-xs text-muted">{workspace.data.description}</p>
          )}
          <p className="ml-auto text-xs text-muted">
            Drag a tab to any edge to re-dock · double-click a tab to collapse
          </p>
        </header>

        <div className="min-h-0 flex-1" ref={containerRef}>
          <DockviewReact
            components={components}
            tabComponents={tabComponents}
            rightHeaderActionsComponent={HeaderActions}
            onReady={onReady}
            // Keep hidden panels mounted. dockview's default
            // ("onlyWhenVisible") destroys a panel when you switch tabs
            // and rebuilds it on return — which for an iframe means a
            // full reload, discarding an in-progress Explore chart or a
            // half-filled form the moment you glance at another tab.
            //
            // The cost is that every panel stays live in the background:
            // embedded dashboards keep their session, and a workspace
            // with many panels holds them all in memory at once.
            defaultRenderer="always"
            className="dockview-theme-light"
          />
        </div>
      </div>
    </CollapseProvider>
  );
}
