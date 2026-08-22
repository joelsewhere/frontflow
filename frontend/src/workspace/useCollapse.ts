import { useCallback, useRef, useState } from "react"
import type { RefObject } from "react"
import type { DockviewGroupPanel } from "dockview"

/**
 * Width/height a collapsed group keeps — its spine.
 *
 * Matches dockview's `--dv-tabs-and-actions-container-height` so a
 * collapsed group is exactly the thickness of its own tab strip.
 */
const COLLAPSED_PX = 35

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

interface StoredSize {
  size: number
  orientation: "horizontal" | "vertical"
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
    (group: DockviewGroupPanel) => {
      const previous = stored.current.get(group.id)

      if (previous) {
        // Restore: clear the constraint first, or setSize is clamped by it.
        group.api.setConstraints({
          maximumWidth: Number.MAX_SAFE_INTEGER,
          maximumHeight: Number.MAX_SAFE_INTEGER,
        })
        if (previous.orientation === "horizontal") {
          group.api.setSize({ width: previous.size })
        } else {
          group.api.setSize({ height: previous.size })
        }
        stored.current.delete(group.id)
        group.element.classList.remove(
          COLLAPSED_CLASS,
          AXIS_CLASS[previous.orientation],
        )
        setCollapsedIds((prev) => {
          const next = new Set(prev)
          next.delete(group.id)
          return next
        })
        return
      }

      const orientation = inferOrientation(group)

      if (orientation === "horizontal") {
        stored.current.set(group.id, { size: group.api.width, orientation })
        group.api.setConstraints({ maximumWidth: COLLAPSED_PX })
        group.api.setSize({ width: COLLAPSED_PX })
      } else {
        stored.current.set(group.id, { size: group.api.height, orientation })
        group.api.setConstraints({ maximumHeight: COLLAPSED_PX })
        group.api.setSize({ height: COLLAPSED_PX })
      }

      group.element.classList.add(COLLAPSED_CLASS, AXIS_CLASS[orientation])
      setCollapsedIds((prev) => new Set(prev).add(group.id))
    },
    [inferOrientation],
  )

  return { toggle, isCollapsed }
}
