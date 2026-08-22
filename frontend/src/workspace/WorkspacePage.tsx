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
import { WorkspaceDashboardPanel, WorkspaceFormPanel } from "./panels";

import "dockview/dist/styles/dockview.css";

interface PanelParams {
  workspaceId: string;
  formId?: string;
  name?: string;
  showFilters?: boolean;
}

const components = {
  form: (props: IDockviewPanelProps<PanelParams>) => (
    <WorkspaceFormPanel formId={props.params.formId as string} />
  ),
  dashboard: (props: IDockviewPanelProps<PanelParams>) => (
    <WorkspaceDashboardPanel
      workspaceId={props.params.workspaceId}
      name={props.params.name as string}
      showFilters={Boolean(props.params.showFilters)}
    />
  ),
};

const tabComponents = { default: PanelTab };

/** Flatten the declared tree into panels, in declaration order. */
function collectPanels(
  block: WorkspaceBlock,
  out: WorkspaceBlock[] = [],
): WorkspaceBlock[] {
  if (block.type === "workspace_form" || block.type === "dashboard") {
    out.push(block);
  }
  for (const child of block.children ?? []) collectPanels(child, out);
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

      const panels = collectPanels(workspace.data.layout);
      const horizontal = rootIsRow(workspace.data.layout);
      let previousKey: string | null = null;

      panels.forEach((panel, index) => {
        const key = panel.id ?? `${panel.type}-${index}`;
        const isForm = panel.type === "workspace_form";

        api.addPanel({
          id: key,
          component: isForm ? "form" : "dashboard",
          title: isForm
            ? ((panel.props.title as string) ??
              (panel.props.form_id as string))
            : (panel.props.name as string),
          params: {
            workspaceId,
            formId: panel.props.form_id as string | undefined,
            name: panel.props.name as string | undefined,
            showFilters: Boolean(panel.props.show_filters),
          },
          ...(previousKey
            ? {
                position: {
                  referencePanel: previousKey,
                  direction: horizontal ? "right" : "below",
                },
              }
            : {}),
        });
        previousKey = key;
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

  const collapseValue = useMemo(
    () => ({ toggle, isCollapsed, resetLayout }),
    [toggle, isCollapsed, resetLayout],
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
            className="dockview-theme-light"
          />
        </div>
      </div>
    </CollapseProvider>
  );
}
