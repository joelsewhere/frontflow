import { createContext, useContext } from "react"
import type { DockviewGroupPanel } from "dockview"

interface CollapseContextValue {
  toggle: (group: DockviewGroupPanel) => void
  isCollapsed: (group: DockviewGroupPanel) => boolean
  resetLayout: () => void
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
