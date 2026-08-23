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
  // Keeps a collapsed group hidden as its membership changes. Disposed
  // on expand.
  const watchers = useRef(new Map<string, { dispose(): void }>())
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
        // Stop maintaining the hidden state before undoing it, or the
        // watcher re-hides the very overlays being restored the next
        // time a panel joins.
        watchers.current.get(group.id)?.dispose()
        watchers.current.delete(group.id)

        for (const overlay of contentOverlays(group)) {
          // Remove rather than blank it, so dockview's own visibility
          // handling governs from here.
          overlay.style.removeProperty("display")
        }

        // Clear the constraint first, or setSize is clamped by it.
        group.api.setConstraints({
          maximumWidth: Number.MAX_SAFE_INTEGER,
          maximumHeight: Number.MAX_SAFE_INTEGER,
          minimumWidth: previous.minimumWidth,
          minimumHeight: previous.minimumHeight,
        })

        const resizeTo = (size: number) => {
          if (previous.orientation === "horizontal") {
            group.api.setSize({ width: size })
          } else {
            group.api.setSize({ height: size })
          }
        }

        resizeTo(previous.size)

        // Then again, a pixel off and back, on the next frame.
        //
        // Content lives in an absolutely positioned overlay that dockview
        // repositions only when a panel reports a dimension change. On
        // expand that report does not reliably reach the overlay, which
        // then keeps the geometry it had while collapsed and the panel
        // comes back blank — until any later resize recomputes it, which
        // is why dragging the edge "fixed" it. Two real size changes,
        // both inside one frame so only the final one is ever painted,
        // guarantee the recompute happens now.
        requestAnimationFrame(() => {
          resizeTo(previous.size - 1)
          resizeTo(previous.size)
        })

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

      // Recorded FIRST, before anything reads it.
      //
      // This entry is both the size to reopen at and the marker that the
      // group is collapsed, and everything below is guarded on it — so
      // writing it last would make the first hide a no-op and leave the
      // content painting over its own spine.
      const minimums = {
        minimumWidth: group.minimumWidth,
        minimumHeight: group.minimumHeight,
      }
      stored.current.set(group.id, {
        size:
          hint?.size ??
          (orientation === "horizontal" ? group.api.width : group.api.height),
        orientation,
        ...minimums,
      })

      // `stored` holds an entry for exactly as long as the group is
      // collapsed — the expand branch above deletes it — so it is the
      // authority on whether any of this still applies.
      //
      // Every deferred step checks it. Disposing a subscription does
      // NOT cancel a microtask it has already queued, so without this a
      // hide queued a moment before an expand lands just after it and
      // blanks the panel that was being reopened: tabs present, content
      // gone, and no way to get it back but a resize.
      const stillCollapsed = () => stored.current.has(group.id)

      const hideContent = () => {
        if (!stillCollapsed()) return
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

      // And again whenever the group changes, along with the size
      // itself.
      //
      // Hiding only the panels present at collapse time is not enough:
      // dragging a tab onto an already-collapsed rail adds a panel whose
      // overlay was never hidden, so it goes on painting at its old
      // size — which is how a collapsed Explore ended up floating over
      // the canvas as a 31px-wide, zero-height strip of text.
      //
      // The size needs re-asserting for a subtler reason. A group's
      // min/max are read from its ACTIVE PANEL when that panel has
      // numeric ones, and fall back to the group's own constraint
      // otherwise (DockviewGroupPanel.minimumWidth). Adding a panel
      // changes the active panel, and the constraints set at collapse
      // time stop deciding the width — the rail drifts to dockview's
      // default group minimum of 100px, which is what a collapsed rail
      // holding several dragged-in tabs was sitting at.
      const pin = () => {
        if (!stillCollapsed()) return
        if (orientation === "horizontal") {
          group.api.setConstraints({
            maximumWidth: COLLAPSED_PX.horizontal,
            minimumWidth: COLLAPSED_PX.horizontal,
          })
          group.api.setSize({ width: COLLAPSED_PX.horizontal })
        } else {
          group.api.setConstraints({
            maximumHeight: COLLAPSED_PX.vertical,
            minimumHeight: COLLAPSED_PX.vertical,
          })
          group.api.setSize({ height: COLLAPSED_PX.vertical })
        }
      }

      const reassert = () => {
        if (!stillCollapsed()) return
        hideContent()
        pin()
        queueMicrotask(() => {
          hideContent()
          pin()
        })
      }

      // Drift, from any cause, is corrected while the group stays
      // collapsed.
      //
      // Restoring a saved layout is the case that needs it: fromJSON
      // scales the grid to the current container and the whole thing
      // settles over the frames AFTER the collapse ran, so the width
      // set at collapse time is overridden by a later pass and nothing
      // fires to notice. Collapsing by hand looked fine because by then
      // the grid was already still.
      //
      // Self-limiting: it only acts when the size is actually wrong, so
      // a correction that lands stops the next one happening.
      // Give up after this many corrections that did not take. A group
      // that cannot reach the collapsed size — a panel inside it with a
      // larger minimum, say — would otherwise be told to resize on
      // every dimension event it emits, forever.
      const MAX_CORRECTIONS = 5
      let failures = 0

      const correctDrift = () => {
        if (!stillCollapsed()) return
        const current =
          orientation === "horizontal" ? group.api.width : group.api.height
        const target =
          orientation === "horizontal"
            ? COLLAPSED_PX.horizontal
            : COLLAPSED_PX.vertical

        if (Math.abs(current - target) <= 1) {
          failures = 0
          return
        }
        if (failures >= MAX_CORRECTIONS) return
        failures += 1
        pin()
      }

      watchers.current.get(group.id)?.dispose()
      const subscriptions = [
        group.model.onDidAddPanel(reassert),
        group.model.onDidRemovePanel(reassert),
        // The active panel is what the width is actually read from, so
        // a tab change alone can move it.
        group.api.onDidActivePanelChange(reassert),
        group.api.onDidDimensionsChange(correctDrift),
      ]
      watchers.current.set(group.id, {
        dispose: () => subscriptions.forEach((s) => s.dispose()),
      })

      if (orientation === "horizontal") {
        // Pinned, not capped. A maximum with a zero minimum only stops
        // the spine growing — dockview stays free to squeeze it
        // NARROWER when space is tight, and fromJSON scales a restored
        // grid proportionally, so a rail saved at one width came back
        // at a fraction of it with its labels clipped. A spine is one
        // exact width; say so.
        group.api.setConstraints({
          maximumWidth: COLLAPSED_PX.horizontal,
          minimumWidth: COLLAPSED_PX.horizontal,
        })
        group.api.setSize({ width: COLLAPSED_PX.horizontal })
      } else {
        group.api.setConstraints({
          maximumHeight: COLLAPSED_PX.vertical,
          minimumHeight: COLLAPSED_PX.vertical,
        })
        group.api.setSize({ height: COLLAPSED_PX.vertical })
      }

      // Two follow-up frames. A grid restored from JSON keeps laying
      // itself out after this returns, and not every pass emits an
      // event the watcher above is listening for. Bounded on purpose —
      // re-pinning indefinitely against a group that genuinely cannot
      // be this size would spin.
      requestAnimationFrame(() => {
        correctDrift()
        requestAnimationFrame(correctDrift)
      })

      setCollapsedIds((prev) => new Set(prev).add(group.id))
    },
    [inferOrientation],
  )

  /**
   * What is collapsed, and how big each one should come back.
   *
   * The expand size lives only in a ref, so it dies with the page. A
   * restored layout brings a collapsed group back at whatever width the
   * grid scaled it to, and collapsing it again would record THAT as the
   * size to expand to — so a rail restored from a save would expand to
   * a spine's width and look stuck. Saving it alongside the layout is
   * what lets a restored collapse still open properly.
   */
  const collapsedState = useCallback(
    (): Array<{ id: string; size: number; orientation: string }> =>
      [...stored.current.entries()].map(([id, entry]) => ({
        id,
        size: entry.size,
        orientation: entry.orientation,
      })),
    [],
  )

  return { toggle, isCollapsed, collapsedState }
}
