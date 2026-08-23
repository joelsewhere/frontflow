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
  getWorkspaceLayout,
  resetWorkspaceLayout,
  saveWorkspaceLayout,
  type WorkspaceBlock,
  type WorkspaceNav,
} from "../lib/api";
import { ActiveSubmissionProvider } from "./ActiveSubmission";
import { CollapseProvider } from "./CollapseContext";
import { HeaderActions } from "./HeaderActions";
import { LayoutControls } from "./LayoutControls";
import {
  GROUP_CHROME_PX,
  buildDockLayout,
  requiredHeightForGrid,
  type LayoutBlock,
} from "./layout";
import { WorkspaceNavPanel } from "./NavPanel";
import { PanelTab } from "./PanelTab";
import {
  bandFor,
  previewWidthFor,
  reconcileLayout,
  unwrapStored,
} from "./reconcile";
import { useCollapse } from "./useCollapse";
import {
  WorkspaceDashboardPanel,
  WorkspaceExplorePanel,
  WorkspaceStoryPanel,
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
  panelId?: string;
  showFilters?: boolean;
  filtersExpanded?: boolean;
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
      panelId={props.params.panelId as string}
      showFilters={Boolean(props.params.showFilters)}
      filtersExpanded={Boolean(props.params.filtersExpanded)}
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
  story: (props: IDockviewPanelProps<PanelParams>) => (
    <WorkspaceStoryPanel
      key={props.params.nonce ?? 0}
      name={props.params.name as string}
      onMeasure={props.params.onMeasure}
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
  if (block.type === "story") {
    // Fall back to the file's stem — the real title lives in the
    // rendered artifact's frontmatter, which the panel has not fetched
    // when the tab is first drawn.
    const name = (props.name as string) ?? "";
    return (
      (props.title as string) ??
      name.split("/").pop()?.replace(/\.xmd$/, "") ??
      "Story"
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
  // panel id -> the least height it may ever have, from its declared
  // `min_height`.
  const panelFloors = useRef<Record<string, number>>({});
  // panel id -> the height a `fit="content"` panel needs. A form
  // measures itself; an embed cannot (its content is cross-origin), so
  // it contributes the height its author declared.
  const [contentHeights, setContentHeights] = useState<
    Readonly<Record<string, number>>
  >({});

  // Mirrored into a ref because build() reads it while constructing
  // the layout, outside the render that owns the state value.
  const contentHeightsRef = useRef<Record<string, number>>({});

  const reportHeight = useCallback((panelId: string, height: number) => {
    const rounded = Math.ceil(height);
    contentHeightsRef.current = {
      ...contentHeightsRef.current,
      [panelId]: rounded,
    };
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

  const { toggle, isCollapsed, collapsedState } = useCollapse(containerRef);

  // Stable across rebuilds: it goes into panel params, and a fresh
  // identity each render would churn every panel that holds it.
  const toggleAuthorTools = useCallback(() => setAuthorTools((on) => !on), []);

  const workspace = useQuery({
    queryKey: ["workspace", workspaceId],
    queryFn: () => getWorkspace(workspaceId as string),
    enabled: Boolean(workspaceId),
  });

  const breakpoints = (workspace.data?.breakpoints ?? []) as number[];

  // Which band an AUTHOR has chosen to arrange. null means "follow the
  // window", which is what every reader does.
  const [editingBand, setEditingBand] = useState<number | null>(null);

  // The band actually in force: the one being edited, or the one this
  // window falls into.
  const [windowBand, setWindowBand] = useState(0);
  useEffect(() => {
    const measure = () => {
      const width = containerRef.current?.clientWidth ?? window.innerWidth;
      setWindowBand(bandFor(width, breakpoints));
    };
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(breakpoints)]);

  const band = editingBand ?? windowBand;

  const saved = useQuery({
    queryKey: ["workspace-layout", workspaceId, band],
    queryFn: () => getWorkspaceLayout(workspaceId as string, band),
    enabled: Boolean(workspaceId),
  });
  const canAuthor = saved.data?.can_author ?? false;

  // Precedence, as one value: this person's arrangement, else the
  // author's, else nothing (and the declared tree stands).
  //
  // Pulled out so `build` can depend on IT. Reading saved.data inside
  // build without listing it made build a stale closure — it was
  // created before the query resolved, so it saw `undefined` forever
  // and every rebuild fell back to the declared layout. Saved layouts
  // were never applied at all.
  const preferredLayout = useMemo(
    () => unwrapStored(saved.data?.user ?? saved.data?.workspace ?? null),
    [saved.data],
  );

  // What is on screen right now, as a content signature.
  //
  // Rebuilding is keyed on this rather than on when the query last
  // resolved. Saving refetches — it has to, so the tier flags are right
  // — and keying on the fetch time meant every save tore the dock down
  // and rebuilt it. Restoring is not perfectly idempotent (a collapsed
  // rail comes back at its stored size, not the pixel width it happens
  // to be), so that rebuild was VISIBLE: the menu column jumped wider
  // the moment you clicked save.
  const appliedSignature = useRef<string | null>(null);
  const preferredSignature = useMemo(
    () => JSON.stringify(preferredLayout),
    [preferredLayout],
  );

  // Saving means the screen has become the truth. Recording it as
  // applied is what stops the refetch that follows from rebuilding
  // into something fractionally different.
  const markApplied = useCallback((stored: Record<string, unknown>) => {
    appliedSignature.current = JSON.stringify(unwrapStored(stored));
  }, []);

  // Suppress saving while a layout is being applied: fromJSON fires
  // onDidLayoutChange, and without this the restore immediately writes
  // itself back — harmless, but it also fires during the initial build,
  // which would persist the DECLARED layout as if a person had chosen
  // it and defeat the fallback entirely.
  const applying = useRef(false);

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
      panelFloors.current = {};

      // The declared tree, as a nested dockview grid.
      //
      // Built as a serialized layout rather than a run of addPanel
      // calls: addPanel places a panel relative to ONE other panel and
      // splits that panel's group, so there is no way to express "below
      // this entire row". A workspace whose root was a Column of Rows
      // came out as a flat stack, the Rows silently discarded.
      const box = {
        width: Math.max(1, Math.round(api.width || containerRef.current?.clientWidth || 1)),
        height: Math.max(1, Math.round(api.height || containerRef.current?.clientHeight || 1)),
      };

      const layout = buildDockLayout(
        workspace.data.layout as LayoutBlock,
        box,
        (block, key) => {
          if (block.type === "dashboard") dashboardPanelIds.current.push(key);

          const fit = block.props.fit === "content" ? "content" : "scroll";
          const declaredHeight = block.props.min_height as number | undefined;
          if (declaredHeight) panelFloors.current[key] = declaredHeight;
          if (fit === "content") {
            contentFitPanels.current.push(key);
            panelFloors.current[key] = panelFloors.current[key] ?? 1;
            // An embed's content is cross-origin and cannot be
            // measured, so its declared height IS its content height.
            // Seed it now; only a form goes on to measure itself.
            if (
              block.type !== "workspace_form" &&
              block.type !== "story" &&
              declaredHeight
            ) {
              reportHeight(key, declaredHeight);
            }
          }

          return {
            id: key,
            contentComponent:
              block.type === "workspace_form"
                ? "form"
                : block.type === "superset_explore"
                  ? "explore"
                  : block.type === "story"
                    ? "story"
                    : "dashboard",
            title: panelTitle(block as WorkspaceBlock),
            // Keep hidden panels mounted, as elsewhere — switching tabs
            // must not destroy an in-progress Explore chart.
            renderer: "always",
            params: {
              workspaceId,
              formId: block.props.form_id as string | undefined,
              name: block.props.name as string | undefined,
              dataset: (block.props.dataset as string | undefined) ?? null,
              panelId: key,
              showFilters: Boolean(block.props.show_filters),
              filtersExpanded: Boolean(block.props.filters_expanded),
              canEdit: canEditDashboards && authorTools,
              onMeasure:
                fit === "content" &&
                (block.type === "workspace_form" || block.type === "story")
                  ? (height: number) => reportHeight(key, height)
                  : undefined,
            },
          };
        },
        contentHeightsRef.current,
      );

      // Navigation is a panel like any other once it is on screen, so a
      // saved layout has to be allowed to keep it — otherwise pruning it
      // strands whatever the person docked beside it in a group of its
      // own, and a second rail appears at the declared edge.
      //
      // Its state is rebuilt here for the same reason every other
      // panel's is: params carry callbacks and permissions, and those do
      // not survive being saved.
      const navStates: Record<string, unknown> = {};
      for (const nav of [workspace.data.navbar, workspace.data.nav]) {
        if (!nav) continue;
        const edge = NAV_EDGE[nav.position];
        navStates[`nav:${nav.kind}`] = {
          id: `nav:${nav.kind}`,
          contentComponent: "nav",
          title: nav.title,
          renderer: "always",
          params: {
            workspaceId,
            nav,
            handle: nav.handle,
            collapseHint: { orientation: edge.orientation, size: nav.size },
            canManage: canEditDashboards,
            authorTools,
            onToggleAuthorTools: toggleAuthorTools,
          },
        };
      }

      // Precedence: this person's arrangement, else the author's, else
      // what the DSL declared. Reconciled against the panels the DSL
      // currently declares, so a saved layout survives an edit instead
      // of being thrown away — see reconcile.ts.
      const declared = {
        ...(layout as { panels?: Record<string, unknown> }),
        panels: {
          ...((layout as { panels?: Record<string, unknown> }).panels ?? {}),
          ...navStates,
        },
      };
      const declaredPanelIds = Object.keys(declared.panels);
      const toApply = reconcileLayout(
        preferredLayout.layout as never,
        declaredPanelIds,
        declared as never,
      );

      applying.current = true;
      appliedSignature.current = preferredSignature;
      try {
        api.fromJSON(toApply as never);

        // Re-collapse what was collapsed. Collapsing is frontflow's
        // mechanism rather than dockview's, so a restored grid brings
        // back a collapsed group's SIZE and leaves it uncollapsed —
        // a narrow strip with its content still painting.
        for (const group of api.groups) {
          const wanted = preferredLayout.collapsedGroups.find(
            (entry) => entry.id === group.id,
          );
          if (!wanted || isCollapsed(group)) continue;
          // The recorded expand size goes in as the hint. Without it
          // toggle records the width the RESTORED grid happens to have
          // — a spine's width, since it was saved collapsed — and the
          // rail would then "expand" to exactly the size it already is.
          toggle(
            group,
            wanted.size
              ? {
                  size: wanted.size,
                  orientation:
                    wanted.orientation === "vertical"
                      ? "vertical"
                      : "horizontal",
                }
              : undefined,
          );
        }
      } finally {
        // After the current frame, so the layout events fromJSON emits
        // are all seen as part of applying rather than as a change.
        requestAnimationFrame(() => {
          applying.current = false;
        });
      }

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

        // Already placed by the restored layout. Re-adding would put a
        // second rail at the declared edge and leave whatever the person
        // docked alongside the first one stranded in a group of its own
        // — which is what pruning the rail out of a saved layout used to
        // do.
        if (api.getPanel(panelId)) continue;

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
      // Without these, build keeps whatever they were when it was first
      // created. preferredLayout going stale meant saved layouts were
      // never applied at all; isCollapsed going stale would restore the
      // wrong groups as collapsed. Same failure, different symptom.
      preferredLayout,
      isCollapsed,
    ],
  );

  // What gets stored: dockview's own serialization, plus which groups
  // were collapsed — see StoredArrangement.
  const snapshot = useCallback(
    (api: DockviewApi): Record<string, unknown> => ({
      dockview: api.toJSON() as unknown as Record<string, unknown>,
      // Ids AND the size each should reopen at — see collapsedState.
      collapsedGroups: collapsedState(),
    }),
    [collapsedState],
  );

  const onReady = useCallback(
    (event: DockviewReadyEvent) => {
      apiRef.current = event.api;
      build(event.api);
    },
    [build],
  );

  // Rebuild when the declared layout changes (a source edit + rescan),
  // and when the saved arrangement for the current band arrives or the
  // band changes. Both feed the same build.
  const layoutSignature = JSON.stringify(workspace.data?.layout ?? null);
  useMemo(() => {
    if (!apiRef.current) return;
    // Already showing this exact arrangement — rebuilding would only
    // perturb it.
    if (appliedSignature.current === preferredSignature) return;
    build(apiRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layoutSignature, band, preferredSignature]);

  // Persist what the person arranged.
  //
  // Debounced: dragging a sash emits a layout change per frame, and
  // every one of them would otherwise be a PUT. Saved to this user's
  // tier — an author writes the shared one deliberately, with a button,
  // because "I moved a panel" and "everyone should see it this way" are
  // different intentions.
  useEffect(() => {
    const api = apiRef.current;
    if (!api || !workspaceId) return;

    let timer: ReturnType<typeof setTimeout> | undefined;
    const subscription = api.onDidLayoutChange(() => {
      if (applying.current) return;
      clearTimeout(timer);
      timer = setTimeout(() => {
        const stored = snapshot(api);
        markApplied(stored);
        saveWorkspaceLayout(workspaceId, band, stored).catch(() => {
          // A failed save is not worth interrupting someone mid-drag.
          // The arrangement is still on screen; it just did not stick.
        });
      }, 600);
    });

    return () => {
      clearTimeout(timer);
      subscription.dispose();
    };
    // `snapshot` reads the CURRENT collapse state, so it has to be
    // current itself — it closes over isCollapsed.
  }, [workspaceId, band, saved.dataUpdatedAt, snapshot]);

  // Hold every panel to the height it asked for.
  //
  // A declared `min_height` counts towards the canvas, so it has to
  // count in the grid too — otherwise the canvas grows to fit a panel
  // that then gets squeezed anyway by a taller sibling, which is how a
  // 560px dashboard ended up as a title bar.
  //
  // `fit="content"` goes further and pins the height, so the panel is
  // exactly its content and cannot be dragged shorter. Either way only
  // `minimumHeight` is constrained: width is left alone, so the
  // horizontal sash still works — a panel showing its content whole is
  // a statement about height, not about how much of the row it
  // deserves. Collapsing still works too; the collapse relaxes this for
  // as long as it lasts, or a minimum taller than a spine would fight
  // it.
  useEffect(() => {
    const api = apiRef.current;
    if (!api) return;

    for (const [id, floor] of Object.entries(panelFloors.current)) {
      const height = Math.max(floor, contentHeights[id] ?? 0);
      if (!height) continue;
      const group = api.getPanel(id)?.api.group;
      if (!group || isCollapsed(group)) continue;

      group.api.setConstraints({ minimumHeight: height });

      // Only a content-fit panel is sized TO its content. A scrolling
      // panel keeps whatever the grid gave it above its floor, so the
      // author's proportions survive.
      // The group's box includes its tab strip, so a group sized to
      // exactly its content renders that content a strip short.
      if (
        contentFitPanels.current.includes(id) &&
        group.api.height < height + GROUP_CHROME_PX
      ) {
        group.api.setSize({ height: height + GROUP_CHROME_PX });
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

  // How tall the canvas must be for the arrangement CURRENTLY on
  // screen — not the declared one.
  //
  // The declaration stops being the truth the moment a panel is
  // dragged: `Column(Row(form, tabs), detail)` needs
  // `max(form, tabs) + detail`, but drag the form out to its own row
  // and the same panels need `form + tabs + detail`. Sizing the canvas
  // from the declaration left the grid hundreds of pixels short of the
  // minimums it was being asked to honour, and a group got squeezed to
  // nothing — which is what made a panel appear to lose its tab strip.
  const [gridHeight, setGridHeight] = useState(0);

  const measureGrid = useCallback(() => {
    const api = apiRef.current;
    if (!api) return;

    const floorOf = (panelId: string) =>
      Math.max(
        panelFloors.current[panelId] ?? 0,
        contentHeightsRef.current[panelId] ?? 0,
      );

    try {
      const { grid } = api.toJSON();
      const needed = requiredHeightForGrid(
        grid.root as never,
        grid.orientation as never,
        floorOf,
      );
      // Only on a real change: resizing the canvas re-lays out the
      // grid, which fires this again.
      setGridHeight((prev) => (Math.abs(prev - needed) < 2 ? prev : needed));
    } catch {
      // A layout mid-drag can serialize inconsistently; the next
      // layout event measures it again.
    }
  }, []);

  useEffect(() => {
    const api = apiRef.current;
    if (!api) return;
    const subscription = api.onDidLayoutChange(() => measureGrid());
    measureGrid();
    return () => subscription.dispose();
  }, [measureGrid, workspace.data]);

  // Re-measure when a form reports a new content height.
  useEffect(() => {
    measureGrid();
  }, [contentHeights, measureGrid]);

  // `max(100%, Npx)`: never shorter than the window, taller when the
  // panels asked for more room than the window has.
  //
  // A `fit="content"` panel therefore brings scrolling with it whether
  // or not the workspace asked for it — a panel promised to show its
  // content whole cannot also be clipped by the window.
  const requiredHeight = Math.max(
    workspace.data?.scroll ? workspace.data.min_canvas_height : 0,
    gridHeight,
  );

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
      {/* Wraps the dock so a form panel's submission is visible to the
          dashboard panels beside it — they are otherwise strangers, and
          a refresh or filter directive would never cross between. */}
      <ActiveSubmissionProvider>
        <div className="flex h-screen flex-col bg-bg">
          <header className="flex flex-shrink-0 items-baseline gap-4 border-b border-border bg-surface px-4 py-2">
            <h1 className="text-[15px] font-semibold">{workspace.data.title}</h1>
            {workspace.data.description && (
              <p className="text-xs text-muted">{workspace.data.description}</p>
            )}
            <p className="ml-auto text-xs text-muted">
              Drag a tab to any edge to re-dock · double-click a tab to collapse
            </p>
            <LayoutControls
              bands={saved.data?.bands ?? [0]}
              breakpoints={breakpoints}
              band={band}
              editingBand={editingBand}
              canAuthor={canAuthor}
              customised={Boolean(saved.data?.user)}
              onEditBand={setEditingBand}
              onSaveForEveryone={async () => {
                const api = apiRef.current;
                if (!api || !workspaceId) return;
                const stored = snapshot(api);
                markApplied(stored);
                await saveWorkspaceLayout(workspaceId, band, stored, true);
                await saved.refetch();
              }}
              onReset={async (forEveryone: boolean) => {
                if (!workspaceId) return;
                await resetWorkspaceLayout(workspaceId, band, forEveryone);
                await saved.refetch();
              }}
            />
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
            {/* While an author is arranging a band, the canvas is
                constrained to that band's floor. Arranging a 900px band
                on a 1500px screen is guesswork: it is seen at 1500 and
                used at 900. The floor is the tight case — a layout that
                works there works everywhere above it. */}
            <div
              className="h-full"
              ref={containerRef}
              style={{
                ...(canvasHeight
                  ? { height: canvasHeight, minHeight: 0 }
                  : undefined),
                ...(editingBand !== null
                  ? {
                      width: previewWidthFor(editingBand),
                      maxWidth: "100%",
                      marginInline: "auto",
                      outline: "1px dashed var(--color-border, #d4d4d4)",
                    }
                  : undefined),
              }}
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
      </ActiveSubmissionProvider>
    </CollapseProvider>
  );
}
