import { useCallback, useRef, useState } from "react"
import type { RefObject } from "react"
import type { DockviewGroupPanel } from "dockview"

/**
 * Thickness a collapsed group keeps — its spine.
 *
 * Against a horizontal edge this matches dockview's
 * `--dv-tabs-and-actions-container-height`, so a group collapsed
 * upwards is exactly the thickness of its own tab strip.
 *
 * A group collapsed sideways gets more. That case is a vertical rail
 * rather than a strip, and 35px was only ever the tab strip's HEIGHT
 * reused as a width — arbitrary, and too narrow for an icon to sit in
 * comfortably. Real icon rails are wider.
 */
const COLLAPSED_PX = {
  horizontal: 44,
  vertical: 35,
} as const

/** Tolerance when comparing a group's size against the container's. */
const EDGE_TOLERANCE_PX = 2

/**
 * Marks the group's DOM element while collapsed.
 *
 * Sizing the group down is not enough on its own: the content container
 * keeps rendering at the new size, so a horizontally collapsed panel
 * still shows a 35px-wide sliver of whatever was inside it. The class
 * is what lets CSS take the content out of the flow entirely, leaving
 * only the spine.
 */
const COLLAPSED_CLASS = "ff-collapsed"
const AXIS_CLASS = {
  horizontal: "ff-collapsed-h",
  vertical: "ff-collapsed-v",
} as const

/**
 * The panel content belonging to a group, as actual DOM elements.
 *
 * Under `renderer: "always"` — which this workspace uses so switching
 * tabs does not destroy an in-progress Explore chart — dockview does NOT
 * render panel content inside the group. It renders it into absolutely
 * positioned `.dv-render-overlay` elements at the dockview root, each
 * sized and placed to mirror its group's content box.
 *
 * So hiding the group's own content container achieves nothing: that
 * container is empty, and the overlay goes on floating over the
 * collapsed group at its old position. It is also what swallows clicks
 * on the spine, since it is absolutely positioned above it.
 */
function contentOverlays(group: DockviewGroupPanel): HTMLElement[] {
  return group.panels
    .map((panel) => panel.view?.content?.element?.parentElement)
    .filter(
      (element): element is HTMLElement =>
        element instanceof HTMLElement &&
        element.classList.contains("dv-render-overlay"),
    )
}

interface StoredSize {
  size: number
  orientation: "horizontal" | "vertical"
  /** Minimums the group had before collapsing, restored on expand.
   *  A `fit="content"` panel carries a minimumHeight far taller than a
   *  spine, and a minimum that outranks the collapsed maximum would
   *  stop the panel closing at all. */
  minimumWidth: number
  minimumHeight: number
}

/**
 * Overrides for collapsing a panel that has not been measured yet.
 *
 * A nav declared `collapsed=True` is closed the moment it is docked,
 * before the grid has laid it out — so its measured width is 0 and both
 * the axis inference and the size to restore to would be nonsense. The
 * declaration already says which edge it is pinned to and how wide it
 * should open, so pass those instead of guessing.
 */
export interface CollapseHint {
  orientation?: "horizontal" | "vertical"
  /** Size to restore to, rather than whatever it measures now. */
  size?: number
}

/**
 * Collapse and restore a dock group by double-clicking its header.
 *
 * Constraints do the work rather than a plain `setSize`, because a bare
 * resize is undone the moment the grid next relayouts (a sibling panel
 * moving, the window resizing). A max-size constraint is what makes the
 * collapsed state stick until it is explicitly cleared.
 */
export function useCollapse(containerRef: RefObject<HTMLElement>) {
  const stored = useRef(new Map<string, StoredSize>())
  // Mirrored into state as well as the ref: the tab and the header
  // actions render differently while collapsed, and a ref alone would
  // not re-render them to say so.
  const [collapsedIds, setCollapsedIds] = useState<ReadonlySet<string>>(
    () => new Set(),
  )

  const isCollapsed = useCallback(
    (group: DockviewGroupPanel) => collapsedIds.has(group.id),
    [collapsedIds],
  )

  /**
   * Which axis is this group free to shrink along?
   *
   * A group sitting beside a sibling collapses horizontally; one stacked
   * above or below a sibling collapses vertically. Compare the group against
   * the container we own — a group spanning the container's full height must
   * be in a row, so its width is the axis with room to give.
   */
  const inferOrientation = useCallback(
    (group: DockviewGroupPanel): "horizontal" | "vertical" => {
      const container = containerRef.current?.getBoundingClientRect()
      if (container) {
        const spansFullHeight =
          Math.abs(group.api.height - container.height) < EDGE_TOLERANCE_PX
        const spansFullWidth =
          Math.abs(group.api.width - container.width) < EDGE_TOLERANCE_PX

        if (spansFullHeight && !spansFullWidth) return "horizontal"
        if (spansFullWidth && !spansFullHeight) return "vertical"
      }

      // Nested both ways, or the only group: collapse along the longer axis.
      return group.api.width >= group.api.height ? "horizontal" : "vertical"
    },
    [containerRef],
  )

  const toggle = useCallback(
    (group: DockviewGroupPanel, hint?: CollapseHint) => {
      const previous = stored.current.get(group.id)

      if (previous) {
        // Put the content back before resizing: the resize is what makes
        // dockview re-measure and re-place the overlay, and it can only
        // measure a content container that is in the flow again.
        group.element.classList.remove(
          COLLAPSED_CLASS,
          AXIS_CLASS[previous.orientation],
        )
        for (const overlay of contentOverlays(group)) {
          overlay.style.display = ""
        }

        // Clear the constraint first, or setSize is clamped by it.
        group.api.setConstraints({
          maximumWidth: Number.MAX_SAFE_INTEGER,
          maximumHeight: Number.MAX_SAFE_INTEGER,
          minimumWidth: previous.minimumWidth,
          minimumHeight: previous.minimumHeight,
        })
        if (previous.orientation === "horizontal") {
          group.api.setSize({ width: previous.size })
        } else {
          group.api.setSize({ height: previous.size })
        }
        stored.current.delete(group.id)
        setCollapsedIds((prev) => {
          const next = new Set(prev)
          next.delete(group.id)
          return next
        })
        return
      }

      const orientation = hint?.orientation ?? inferOrientation(group)

      // Hide before resizing, so nothing is left painting over the spine
      // at the size the group used to be. `display: none` keeps the DOM
      // and its state — the same mechanism dockview itself uses for
      // hidden panels, which is why a collapsed Explore comes back
      // exactly as it was.
      group.element.classList.add(COLLAPSED_CLASS, AXIS_CLASS[orientation])

      const hideContent = () => {
        for (const overlay of contentOverlays(group)) {
          overlay.style.display = "none"
        }
      }
      hideContent()
      // Again once the current stack frame drains. A nav declared
      // `collapsed=True` is closed the instant it is docked, and
      // dockview attaches its overlay in a microtask — so on that first
      // pass there is nothing to hide yet.
      queueMicrotask(hideContent)

      const minimums = {
        minimumWidth: group.minimumWidth,
        minimumHeight: group.minimumHeight,
      }

      if (orientation === "horizontal") {
        stored.current.set(group.id, {
          size: hint?.size ?? group.api.width,
          orientation,
          ...minimums,
        })
        group.api.setConstraints({
          maximumWidth: COLLAPSED_PX.horizontal,
          minimumWidth: 0,
        })
        group.api.setSize({ width: COLLAPSED_PX.horizontal })
      } else {
        stored.current.set(group.id, {
          size: hint?.size ?? group.api.height,
          orientation,
          ...minimums,
        })
        group.api.setConstraints({
          maximumHeight: COLLAPSED_PX.vertical,
          minimumHeight: 0,
        })
        group.api.setSize({ height: COLLAPSED_PX.vertical })
      }

      setCollapsedIds((prev) => new Set(prev).add(group.id))
    },
    [inferOrientation],
  )

  return { toggle, isCollapsed }
}
