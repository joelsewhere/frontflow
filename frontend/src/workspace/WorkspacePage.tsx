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

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DockviewReact } from "dockview";
import type {
  DockviewApi,
  DockviewReadyEvent,
  IDockviewPanelProps,
} from "dockview";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";

import {
  getWorkspace,
  type WorkspaceBlock,
  type WorkspaceNav,
} from "../lib/api";
import { CollapseProvider } from "./CollapseContext";
import { HeaderActions } from "./HeaderActions";
import { WorkspaceNavPanel } from "./NavPanel";
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
  /** Navigation panels carry their declaration, so the panel and its
   *  collapsed spine both render what the author asked for. */
  nav?: WorkspaceNav;
  handle?: WorkspaceNav["handle"];
  /** Lets the tab collapse a nav it has never seen laid out. */
  collapseHint?: { orientation?: "horizontal" | "vertical"; size?: number };
  canManage?: boolean;
  authorTools?: boolean;
  onToggleAuthorTools?: () => void;
  /** Set on a `fit="content"` form panel: reports the height the form
   *  actually needs, so the grid can be sized to it. */
  onMeasure?: (height: number) => void;
}

const components = {
  form: (props: IDockviewPanelProps<PanelParams>) => (
    <WorkspaceFormPanel
      key={props.params.nonce ?? 0}
      formId={props.params.formId as string}
      onMeasure={props.params.onMeasure}
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
  nav: (props: IDockviewPanelProps<PanelParams>) => (
    <WorkspaceNavPanel
      key={props.params.nonce ?? 0}
      nav={props.params.nav as WorkspaceNav}
      canManage={Boolean(props.params.canManage)}
      authorTools={Boolean(props.params.authorTools)}
      onToggleAuthorTools={props.params.onToggleAuthorTools ?? (() => {})}
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

/**
 * The tab renderer for every panel.
 *
 * This has to be `defaultTabComponent`, not `tabComponents`. dockview
 * resolves a tab as `panel.tabComponent ?? options.defaultTabComponent`
 * and consults the `tabComponents` record only by that resolved name —
 * so a `tabComponents={{ default: ... }}` that no panel asks for by name
 * is never reached, and every panel silently gets dockview's built-in
 * tab instead. Silently is the operative word: there is no warning,
 * the tabs look right, and only the behaviour attached to them
 * (double-click to collapse, click a spine to expand) goes missing.
 */
const defaultTabComponent = PanelTab;

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
 * How tall the grid must be for every panel to get the room it needs.
 *
 * The same arithmetic the server does for declared `min_height`s —
 * stacking adds, splitting takes the tallest — but over MEASURED
 * heights, which only exist once a `fit="content"` form has rendered
 * and said how big it actually is. The server cannot know that number,
 * and the server's own total is the floor this starts from.
 */
function requiredCanvasHeight(
  block: WorkspaceBlock,
  measured: Readonly<Record<string, number>>,
): number {
  if (PANEL_TYPES.has(block.type)) {
    const id = block.id;
    const declared = (block.props.min_height as number | null) ?? 0;
    return Math.max(id ? (measured[id] ?? 0) : 0, declared);
  }

  const children = block.children ?? [];
  if (children.length === 0) return 0;

  const heights = children.map((child) => requiredCanvasHeight(child, measured));
  return block.type === "column"
    ? heights.reduce((total, h) => total + h, 0)
    : Math.max(...heights);
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

/**
 * Where a nav docks, and which way it collapses.
 *
 * A nav pinned to a side gives up width when it closes; one across the
 * top or bottom gives up height. dockview calls those "above"/"below"
 * rather than top/bottom, hence the translation.
 */
const NAV_EDGE = {
  left: { direction: "left", orientation: "horizontal" },
  right: { direction: "right", orientation: "horizontal" },
  top: { direction: "above", orientation: "vertical" },
  bottom: { direction: "below", orientation: "vertical" },
} as const;

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

/**
 * Whether the author's dashboard controls are showing.
 *
 * Persisted per workspace so a reload does not drop you out of a preview
 * mid-presentation. It is a display preference only: the server decides
 * who may edit, and turning this on cannot grant anything the
 * workspace's ACL withheld.
 */
function authorToolsKey(workspaceId: string | undefined): string | null {
  return workspaceId ? `frontflow.workspace.${workspaceId}.authorTools` : null;
}

function readAuthorTools(workspaceId: string | undefined): boolean {
  const key = authorToolsKey(workspaceId);
  if (!key) return true;
  try {
    // Default on: an author who has never touched the toggle should see
    // the tools they had before it existed.
    return window.localStorage.getItem(key) !== "0";
  } catch {
    return true;
  }
}

function writeAuthorTools(workspaceId: string, value: boolean): void {
  const key = authorToolsKey(workspaceId) as string;
  try {
    window.localStorage.setItem(key, value ? "1" : "0");
  } catch {
    // Private browsing or a full quota — the toggle still works for this
    // session, it just does not survive a reload.
  }
}

export default function WorkspacePage() {
  const { workspaceId } = useParams<{ workspaceId: string }>();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const apiRef = useRef<DockviewApi | null>(null);
  // Which panels carry a dashboard, so the author-tools toggle can reach
  // exactly those and leave forms and Explore untouched.
  const dashboardPanelIds = useRef<string[]>([]);
  // Navigation panels, so the develop switch inside one stays in step
  // with the dashboards it governs.
  const navPanelIds = useRef<string[]>([]);
  // Panels declared `fit="content"`: sized to their content and unable
  // to be dragged shorter.
  const contentFitPanels = useRef<string[]>([]);
  // panel id -> the height a `fit="content"` panel needs. A form
  // measures itself; an embed cannot (its content is cross-origin), so
  // it contributes the height its author declared.
  const [contentHeights, setContentHeights] = useState<
    Readonly<Record<string, number>>
  >({});

  const reportHeight = useCallback((panelId: string, height: number) => {
    const rounded = Math.ceil(height);
    setContentHeights((prev) =>
      // A one-pixel wobble from a re-layout must not loop us back
      // through a resize that produces another wobble.
      Math.abs((prev[panelId] ?? 0) - rounded) < 2
        ? prev
        : { ...prev, [panelId]: rounded },
    );
  }, []);

  const [authorTools, setAuthorTools] = useState(() =>
    readAuthorTools(workspaceId),
  );

  // React-router reuses this component when only the :workspaceId param
  // changes, so without this the preference would follow you from one
  // workspace to the next — and the effect below would then write it
  // over whatever the new workspace had stored.
  const authorToolsFor = useRef(workspaceId);
  if (authorToolsFor.current !== workspaceId) {
    authorToolsFor.current = workspaceId;
    setAuthorTools(readAuthorTools(workspaceId));
  }

  useEffect(() => {
    if (workspaceId) writeAuthorTools(workspaceId, authorTools);
  }, [workspaceId, authorTools]);

  const { toggle, isCollapsed } = useCollapse(containerRef);

  // Stable across rebuilds: it goes into panel params, and a fresh
  // identity each render would churn every panel that holds it.
  const toggleAuthorTools = useCallback(() => setAuthorTools((on) => !on), []);

  const workspace = useQuery({
    queryKey: ["workspace", workspaceId],
    queryFn: () => getWorkspace(workspaceId as string),
    enabled: Boolean(workspaceId),
  });

  // The ACL answer: may this person edit the workspace's dashboards at
  // all. The toggle below can only ever hide what this permits — it is a
  // presentation control, never a way to gain access.
  const canEditDashboards = workspace.data?.can_edit_dashboards ?? false;

  const build = useCallback(
    (api: DockviewApi) => {
      if (!workspace.data || !workspaceId) return;
      api.clear();
      dashboardPanelIds.current = [];
      contentFitPanels.current = [];

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
        if (block.type === "dashboard") dashboardPanelIds.current.push(key);

        const fit = block.props.fit === "content" ? "content" : "scroll";
        const declaredHeight = block.props.min_height as number | undefined;
        if (fit === "content") {
          contentFitPanels.current.push(key);
          // An embed's content is cross-origin and cannot be measured,
          // so its declared height IS its content height. Seed it now;
          // only a form goes on to measure itself.
          if (block.type !== "workspace_form" && declaredHeight) {
            reportHeight(key, declaredHeight);
          }
        }

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
            canEdit: canEditDashboards && authorTools,
            onMeasure:
              fit === "content" && block.type === "workspace_form"
                ? (height: number) => reportHeight(key, height)
                : undefined,
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

      // Navigation docks last, against an edge of the whole grid. Added
      // earlier it would become the anchor the panels above position
      // themselves against, and the declared arrangement would come out
      // wrapped around the nav instead of beside it.
      navPanelIds.current = [];
      for (const nav of [workspace.data.navbar, workspace.data.nav]) {
        if (!nav) continue;

        const edge = NAV_EDGE[nav.position];
        const collapseHint = {
          orientation: edge.orientation,
          size: nav.size,
        };
        const panelId = `nav:${nav.kind}`;
        navPanelIds.current.push(panelId);

        api.addPanel({
          id: panelId,
          component: "nav",
          title: nav.title,
          params: {
            workspaceId,
            nav,
            handle: nav.handle,
            collapseHint,
            canManage: canEditDashboards,
            authorTools,
            onToggleAuthorTools: toggleAuthorTools,
          },
          position: { direction: edge.direction },
        });

        const panel = api.getPanel(panelId);
        if (!panel) continue;

        if (edge.orientation === "horizontal") {
          panel.api.setSize({ width: nav.size });
        } else {
          panel.api.setSize({ height: nav.size });
        }

        // Pre-closed, if that is how it was declared. The hint carries
        // the axis and the open size, because at this moment the panel
        // has not been laid out and measuring it would yield zero.
        if (nav.collapsed && panel.api.group) {
          toggle(panel.api.group, collapseHint);
        }
      }
    },
    [
      workspace.data,
      workspaceId,
      canEditDashboards,
      authorTools,
      toggle,
      toggleAuthorTools,
      reportHeight,
    ],
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

  // Give every `fit="content"` panel exactly the height it asked for,
  // and stop it being dragged shorter.
  //
  // Only `minimumHeight` is constrained. Width is left alone, so the
  // horizontal sash still works — a panel that shows its content whole
  // is a statement about height, not about how much of the row it
  // deserves. Collapsing still works too; the collapse relaxes this
  // constraint for as long as it lasts, or a minimum taller than the
  // spine would fight it.
  useEffect(() => {
    const api = apiRef.current;
    if (!api) return;

    for (const id of contentFitPanels.current) {
      const height = contentHeights[id];
      if (!height) continue;
      const group = api.getPanel(id)?.api.group;
      if (!group || isCollapsed(group)) continue;

      group.api.setConstraints({ minimumHeight: height });
      if (Math.abs(group.api.height - height) >= 2) {
        group.api.setSize({ height });
      }
    }
  }, [contentHeights, isCollapsed]);

  // Toggling must not remount anything: dockview merges parameters and
  // re-renders the same component instance, so an Explore chart or a
  // half-filled form in another tab is untouched. Rebuilding the layout
  // instead would discard all of it.
  useEffect(() => {
    const api = apiRef.current;
    if (!api) return;
    const canEdit = canEditDashboards && authorTools;
    for (const id of dashboardPanelIds.current) {
      api.getPanel(id)?.api.updateParameters({ canEdit });
    }
    // The switch lives in the nav, so the nav has to be told too.
    for (const id of navPanelIds.current) {
      api.getPanel(id)?.api.updateParameters({ authorTools });
    }
  }, [canEditDashboards, authorTools]);

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

  // `max(100%, Npx)`: never shorter than the window, taller when the
  // panels asked for more room than the window has.
  //
  // A `fit="content"` panel therefore brings scrolling with it whether
  // or not the workspace asked for it — a panel promised to show its
  // content whole cannot also be clipped by the window.
  const requiredHeight = useMemo(() => {
    if (!workspace.data) return 0;
    const declared = workspace.data.scroll
      ? workspace.data.min_canvas_height
      : 0;
    return Math.max(
      declared,
      requiredCanvasHeight(workspace.data.layout, contentHeights),
    );
  }, [workspace.data, contentHeights]);

  const canvasHeight =
    requiredHeight > 0 ? `max(100%, ${requiredHeight}px)` : null;

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

        {/* A dock normally fills its container exactly and never
            scrolls. To let a workspace run past the fold, the canvas is
            given a height of its own and THIS element scrolls it —
            dockview lays out to whatever it is handed.

            Panel content sits in absolutely positioned overlays, but
            dockview places them as the difference between two page
            positions, so a scroll offset cancels on both sides and the
            content stays where it belongs. */}
        <div
          className={
            canvasHeight ? "min-h-0 flex-1 overflow-y-auto" : "min-h-0 flex-1"
          }
        >
          <div
            className="h-full"
            ref={containerRef}
            style={
              canvasHeight ? { height: canvasHeight, minHeight: 0 } : undefined
            }
          >
            <DockviewReact
              components={components}
              defaultTabComponent={defaultTabComponent}
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
      </div>
    </CollapseProvider>
  );
}
