import { useEffect, useRef } from "react";
import type { IDockviewPanelHeaderProps } from "dockview";

import type { WorkspaceNavHandle } from "../lib/api";
import { useCollapseContext } from "./CollapseContext";

/** Where along the collapsed edge the handle sits. */
const ALIGNMENT: Record<WorkspaceNavHandle["position"], string> = {
  start: "justify-start",
  center: "justify-center",
  end: "justify-end",
};


/** Properties this component writes onto dockview's own elements. */
const RAIL_STYLES: Array<[string, string]> = [
  ["height", "100%"],
  ["width", "100%"],
  ["flex-direction", "column"],
  ["align-items", "stretch"],
  ["justify-content", "flex-start"],
  ["overflow", "visible"],
  ["padding", "0"],
  ["flex-grow", "0"],
];

/**
 * Shape a collapsed group's tab strip into a vertical rail.
 *
 * Done imperatively, which is not the obvious choice — but the elements
 * between this component and the rail are dockview's, styled by
 * dockview's own stylesheet, and a rule aimed at them has now lost the
 * cascade three separate ways: to source order (equal specificity, and
 * the bundler decides), to Tailwind tree-shaking a layered rule whose
 * class it could not find in any source file, and to
 * `.dv-single-tab.dv-full-width-single-tab` selectors being more
 * specific than anything reasonable to write here.
 *
 * Inline styles beat all three. Only the properties written here are
 * removed on cleanup, so dockview's own inline sizing is left alone.
 */
function useRailShape(
  ref: React.RefObject<HTMLElement>,
  active: boolean,
): void {
  useEffect(() => {
    const own = ref.current;
    if (!own) return;

    const strip = own.closest<HTMLElement>(".dv-tabs-and-actions-container");
    const tabs = own.closest<HTMLElement>(".dv-tabs-container");
    const tab = own.closest<HTMLElement>(".dv-tab");
    if (!strip || !tabs || !tab) return;

    if (active) {
      // The strip becomes the whole rail rather than a one-line header.
      strip.style.setProperty("height", "100%");
      strip.style.setProperty("flex-direction", "column");
      strip.style.setProperty("align-items", "stretch");
      // Its tab list runs down it, and must not clip a handle that is
      // taller than a tab strip expects.
      tabs.style.setProperty("width", "100%");
      tabs.style.setProperty("flex-direction", "column");
      tabs.style.setProperty("overflow", "visible");
      tabs.style.setProperty("flex-grow", "0");
      // And the tab spans the rail, so centring inside it centres in
      // the rail rather than in something narrower.
      tab.style.setProperty("width", "100%");
      tab.style.setProperty("padding", "0");
    }

    return () => {
      for (const [property] of RAIL_STYLES) {
        strip.style.removeProperty(property);
        tabs.style.removeProperty(property);
        tab.style.removeProperty(property);
      }
    };
  }, [ref, active]);
}

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
  const ownRef = useRef<HTMLDivElement | null>(null);

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

  // Only a sideways collapse becomes a rail; a navbar collapsed
  // upwards is already the shape a tab strip is built for.
  const vertical = isVerticalSpine(props);
  useRailShape(ownRef, collapsed && vertical);

  if (collapsed) {
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
        ref={ownRef}
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
      ref={ownRef}
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
