import { createContext, useContext } from "react"
import type { DockviewGroupPanel } from "dockview"

import type { CollapseHint } from "./useCollapse"

interface CollapseContextValue {
  toggle: (group: DockviewGroupPanel, hint?: CollapseHint) => void
  isCollapsed: (group: DockviewGroupPanel) => boolean
  resetLayout: () => void
  /** Remount one panel's contents. Panels stay mounted when hidden, so
   *  nothing reloads on its own — this is the way to get a fresh frame
   *  after signing in to Superset, or to re-read a form. */
  reloadPanel: (panelId: string) => void
}

const CollapseContext = createContext<CollapseContextValue | null>(null)

export const CollapseProvider = CollapseContext.Provider

export function useCollapseContext(): CollapseContextValue {
  const value = useContext(CollapseContext)
  if (!value) {
    throw new Error('useCollapseContext must be used within a CollapseProvider')
  }
  return value
}
