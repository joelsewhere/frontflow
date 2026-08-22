import type { IDockviewPanelHeaderProps } from "dockview";

import { useCollapseContext } from "./CollapseContext";

/**
 * A panel's tab — which is also the handle you drag to re-dock it, and
 * so the natural target for double-click-to-collapse.
 *
 * A custom tab is used rather than dockview's default because the
 * default binds its own double-click behaviour.
 */
export function PanelTab(props: IDockviewPanelHeaderProps) {
  const { toggle, isCollapsed } = useCollapseContext();
  const group = props.api.group;
  const collapsed = group ? isCollapsed(group) : false;

  const handleDoubleClick = (event: React.MouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
    if (group) toggle(group);
  };

  // Collapsed against a vertical edge the tab IS the spine, so the label
  // turns to run down it. A single click expands: at 35px wide there is
  // nothing else to aim at, and requiring a double-click to undo a
  // double-click reads as the panel being stuck.
  const handleClick = collapsed
    ? (event: React.MouseEvent) => {
        event.preventDefault();
        event.stopPropagation();
        if (group) toggle(group);
      }
    : undefined;

  return (
    <div
      className={
        collapsed
          ? "flex h-full w-full cursor-pointer select-none items-center justify-center py-2 text-sm"
          : "flex h-full cursor-pointer select-none items-center whitespace-nowrap px-3 text-sm"
      }
      onClick={handleClick}
      onDoubleClick={handleDoubleClick}
      title={
        collapsed
          ? `${props.api.title} — click to expand`
          : "Drag to re-dock · double-click to collapse"
      }
    >
      <span
        className="overflow-hidden text-ellipsis"
        style={
          collapsed
            ? { writingMode: "vertical-rl", textOrientation: "mixed" }
            : undefined
        }
      >
        {props.api.title}
      </span>
    </div>
  );
}
