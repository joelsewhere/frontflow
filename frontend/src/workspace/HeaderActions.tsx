import type { IDockviewHeaderActionsProps } from "dockview";

import { useCollapseContext } from "./CollapseContext";

/**
 * Right-aligned actions in each group's header: an explicit collapse
 * control — double-click is only discoverable once you know about it —
 * and a layout reset, which is the escape hatch when an arrangement
 * gets into a state the user cannot undo by dragging.
 */
export function HeaderActions(props: IDockviewHeaderActionsProps) {
  const { toggle, isCollapsed, resetLayout } = useCollapseContext();
  const collapsed = isCollapsed(props.group);

  return (
    <div className="flex h-full items-center gap-0.5 pr-1.5">
      <button
        type="button"
        className="rounded px-1.5 py-1 text-xs leading-none text-muted hover:bg-surface hover:text-ink"
        onClick={() => toggle(props.group)}
        title={collapsed ? "Expand panel" : "Collapse panel"}
        aria-label={collapsed ? "Expand panel" : "Collapse panel"}
      >
        {collapsed ? "⟩" : "⟨"}
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
