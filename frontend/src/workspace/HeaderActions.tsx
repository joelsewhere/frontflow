import type { IDockviewHeaderActionsProps } from "dockview";

import { useCollapseContext } from "./CollapseContext";

/**
 * Right-aligned actions in each group's header, available on every panel
 * regardless of what it contains or who is viewing:
 *
 *   - Reload the active panel. Panels stay mounted when hidden, so
 *     nothing refreshes on its own; this is how you get a fresh frame
 *     after signing in to Superset, or re-read a form.
 *   - Collapse, since double-click on a tab is only discoverable once
 *     you know about it.
 *   - Reset the layout — the escape hatch when an arrangement gets into
 *     a state dragging cannot undo.
 */
export function HeaderActions(props: IDockviewHeaderActionsProps) {
  const { toggle, isCollapsed, resetLayout, reloadPanel } =
    useCollapseContext();
  const collapsed = isCollapsed(props.group);
  const activePanelId = props.activePanel?.id;

  // Collapsed, the group is only as thick as its spine — there is no
  // room for three controls, and reloading or resetting something you
  // cannot see is not a thing anyone reaches for. The tab itself
  // expands on click, so nothing is stranded.
  if (collapsed) return null;

  return (
    <div className="flex h-full items-center gap-0.5 pr-1.5">
      {activePanelId && (
        <button
          type="button"
          className="rounded px-1.5 py-1 text-xs leading-none text-muted hover:bg-surface hover:text-ink"
          onClick={() => reloadPanel(activePanelId)}
          title="Reload this panel"
          aria-label="Reload this panel"
        >
          ⟳
        </button>
      )}
      <button
        type="button"
        className="rounded px-1.5 py-1 text-xs leading-none text-muted hover:bg-surface hover:text-ink"
        onClick={() => toggle(props.group)}
        title="Collapse panel"
        aria-label="Collapse panel"
      >
        ⟨
      </button>
      <button
        type="button"
        className="rounded px-1.5 py-1 text-xs leading-none text-muted hover:bg-surface hover:text-ink"
        onClick={resetLayout}
        title="Reset layout"
        aria-label="Reset layout"
      >
        ⟲
      </button>
    </div>
  );
}
