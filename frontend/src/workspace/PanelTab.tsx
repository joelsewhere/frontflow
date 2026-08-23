import { useEffect, useRef, useState } from "react";
import type { IDockviewPanelHeaderProps } from "dockview";

import type { WorkspaceNavHandle } from "../lib/api";
import { useCollapseContext } from "./CollapseContext";
import { groupOf, railShapeMutations } from "./railShape";

/** Where along the collapsed edge the handle sits. */
const ALIGNMENT: Record<WorkspaceNavHandle["position"], string> = {
  start: "justify-start",
  center: "justify-center",
  end: "justify-end",
};


/**
 * Shape a collapsed group's tab strip into a vertical rail.
 *
 * The walk itself lives in railShape.ts, as data, so it can be tested.
 * What is here is WHEN to apply it, which is the part that has been
 * wrong: re-parenting.
 *
 * A tab dragged into an already-collapsed rail mounts with the rail
 * already active, so this effect runs exactly once — and dockview moves
 * the tab into its final position as part of the drop, AFTER that run.
 * The styles land on the ancestors the tab had a moment ago, its real
 * ancestors never get them, and the handle sits uncentred until an
 * expand/collapse happens to re-run the walk against the right chain.
 *
 * So the group is observed for structural change and the walk re-runs.
 * `childList` only: watching attributes would see this function's own
 * inline styles and loop.
 *
 * Previous inline values are restored rather than removed. Some of these
 * elements carry their own inline sizing — `.dockview-react-part` is
 * born with `width: 100%` — and deleting the property outright would
 * take dockview's with it.
 */
function useRailShape(
  ref: React.RefObject<HTMLElement>,
  active: boolean,
): void {
  const touched = useRef<Array<[HTMLElement, string, string]>>([]);

  useEffect(() => {
    const restore = () => {
      for (const [element, property, previous] of touched.current) {
        if (previous) element.style.setProperty(property, previous);
        else element.style.removeProperty(property);
      }
      touched.current = [];
    };

    const apply = () => {
      // Always from a clean slate, so a re-parent undoes the styles on
      // the ancestors the tab has left behind before shaping the new
      // ones. Without this, a dragged tab would leave a stretched
      // wrapper behind it in its old group.
      restore();

      const own = ref.current;
      if (!own || !active) return;

      for (const { element, property, value } of railShapeMutations(own)) {
        touched.current.push([
          element,
          property,
          element.style.getPropertyValue(property),
        ]);
        element.style.setProperty(property, value);
      }
    };

    apply();

    const own = ref.current;
    if (!own || !active) return restore;

    const group = groupOf(own);
    if (!group) return restore;

    const observer = new MutationObserver(() => apply());
    observer.observe(group, { childList: true, subtree: true });
    return () => {
      observer.disconnect();
      restore();
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
  const vertical = useVerticalSpine(props);
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
/**
 * Whether this tab sits on a vertical spine.
 *
 * A declared `collapseHint` settles it. Without one the group's own
 * proportions do — and those are read during render, so the answer has
 * to be RECOMPUTED when they change.
 *
 * That is not a detail. A tab dragged into an already-collapsed rail
 * renders before dockview resizes the group, so the fallback reads the
 * pre-drop proportions, decides the group is wide rather than tall, and
 * lays the label out horizontally. Nothing re-rendered it afterwards,
 * so it stayed that way until an expand/collapse forced a new render —
 * which is exactly the shape of the centring bug one layer up.
 */
function useVerticalSpine(props: IDockviewPanelHeaderProps): boolean {
  const group = props.api.group;
  const [, bump] = useState(0);

  useEffect(() => {
    const invalidate = () => bump((n) => n + 1);
    // Two different things make the answer stale, and the second is the
    // one that bites. Dragging a tab into an already-collapsed rail
    // does not resize anything — it moves the panel to a DIFFERENT
    // group, and `props.api.group` still points at the wide group it
    // came from when this first renders. Reading proportions off that
    // says "wide", so the label is laid out horizontally and stays
    // there, because nothing re-rendered it.
    const subscriptions = [
      props.api.onDidGroupChange(invalidate),
      group?.api.onDidDimensionsChange(invalidate),
    ];
    return () => subscriptions.forEach((s) => s?.dispose());
  }, [props.api, group]);

  const orientation = (
    props.params?.collapseHint as { orientation?: string } | undefined
  )?.orientation;
  if (orientation) return orientation === "horizontal";
  return group ? group.api.height > group.api.width : true;
}
