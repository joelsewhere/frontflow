import type { IDockviewPanelHeaderProps } from "dockview";

import type { WorkspaceNavHandle } from "../lib/api";
import { useCollapseContext } from "./CollapseContext";

/** Where along the collapsed edge the handle sits. */
const ALIGNMENT: Record<WorkspaceNavHandle["position"], string> = {
  start: "justify-start",
  center: "justify-center",
  end: "justify-end",
};

/**
 * A panel's tab — which is also the handle you drag to re-dock it, and
 * so the natural target for double-click-to-collapse.
 *
 * A custom tab is used rather than dockview's default because the
 * default binds its own double-click behaviour.
 *
 * For a nav this tab IS the navigation's closed state: a nav spends most
 * of its life collapsed, so the spine is what people actually see and
 * aim at. `workspace.Handle(...)` is what an author sets to make it look
 * like navigation rather than a shut panel.
 */
export function PanelTab(props: IDockviewPanelHeaderProps) {
  const { toggle, isCollapsed } = useCollapseContext();
  const group = props.api.group;
  const collapsed = group ? isCollapsed(group) : false;
  const handle = props.params?.handle as WorkspaceNavHandle | undefined;

  const collapseHint = props.params?.collapseHint as
    | { orientation?: "horizontal" | "vertical"; size?: number }
    | undefined;

  const handleDoubleClick = (event: React.MouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
    if (group) toggle(group, collapseHint);
  };

  // Collapsed, a single click expands: at spine width there is nothing
  // else to aim at, and needing a double-click to undo a double-click
  // reads as the panel being stuck.
  const handleClick = collapsed
    ? (event: React.MouseEvent) => {
        event.preventDefault();
        event.stopPropagation();
        if (group) toggle(group);
      }
    : undefined;

  const label = handle?.label ?? props.api.title;
  const alignment = ALIGNMENT[handle?.position ?? "start"];

  if (collapsed) {
    // The spine. A vertical edge turns the label to run down it; a
    // navbar collapsed against the top keeps it horizontal.
    const vertical = isVerticalSpine(props);
    const icon = handle?.icon;

    // A side spine is 35px wide — an icon stays legible at that width,
    // rotated text is marginal, and stacking both makes each of them
    // unreadable. So on a side spine an icon, where the author gave
    // one, IS the handle; the label becomes its tooltip and its
    // accessible name. Without an icon the rotated label gets the whole
    // width to itself.
    const iconOnly = vertical && Boolean(icon);

    return (
      <div
        className={`flex w-full cursor-pointer select-none items-center gap-2 py-3 text-sm ${alignment} ${
          vertical ? "flex-col px-0" : "h-full flex-row px-2"
        }`}
        onClick={handleClick}
        onDoubleClick={handleDoubleClick}
        title={`${label} — click to expand`}
        aria-label={`${label} — expand`}
      >
        {icon && (
          <span aria-hidden className={iconOnly ? "text-base leading-none" : ""}>
            {icon}
          </span>
        )}
        {!iconOnly && (
          <span
            className={
              vertical
                ? "whitespace-nowrap tracking-wide"
                : "overflow-hidden text-ellipsis whitespace-nowrap"
            }
            style={
              vertical
                ? { writingMode: "vertical-rl", textOrientation: "mixed" }
                : undefined
            }
          >
            {label}
          </span>
        )}
      </div>
    );
  }

  return (
    <div
      className="flex h-full cursor-pointer select-none items-center gap-1.5 whitespace-nowrap px-3 text-sm"
      onDoubleClick={handleDoubleClick}
      title="Drag to re-dock · double-click to collapse"
    >
      {handle?.icon && <span aria-hidden>{handle.icon}</span>}
      <span className="overflow-hidden text-ellipsis">{props.api.title}</span>
    </div>
  );
}

/**
 * Whether the collapsed spine runs vertically.
 *
 * A group pinned to a side collapses to a tall, narrow spine and its
 * label reads better rotated; one pinned to the top stays wide, and
 * rotating there would make it unreadable for no gain.
 */
function isVerticalSpine(props: IDockviewPanelHeaderProps): boolean {
  const orientation = (
    props.params?.collapseHint as { orientation?: string } | undefined
  )?.orientation;
  if (orientation) return orientation === "horizontal";
  const group = props.api.group;
  return group ? group.api.height > group.api.width : true;
}
