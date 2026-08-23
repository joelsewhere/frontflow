/**
 * Merging a saved arrangement with the panels the DSL declares.
 *
 * A saved layout is dockview's serialized grid. It is NOT authoritative
 * about which panels exist — the DSL is. Treating it as authoritative
 * would either resurrect panels an author deleted or hide ones they
 * added, depending on which side you trusted.
 *
 * So the saved layout decides ARRANGEMENT and the DSL decides
 * MEMBERSHIP, and this reconciles the two:
 *
 *   - a panel in both keeps the position it was dragged to
 *   - a panel only in the saved layout is dropped, and any group left
 *     empty by that is pruned
 *   - a panel only in the DSL is added where the DSL puts it
 *   - a saved layout with nothing recognisable left falls back to the
 *     declared default entirely
 *
 * The alternative — discarding every saved layout whenever the source
 * changes — means renaming a dashboard costs everyone their workspace.
 */

/** dockview's serialized grid, as much of it as we need to reason about. */
export interface GridNode {
  type: "branch" | "leaf";
  data?: GridNode[] | { views?: string[]; activeView?: string; id?: string };
  size?: number;
  visible?: boolean;
}

export interface SerializedGrid {
  root?: GridNode;
  width?: number;
  height?: number;
  orientation?: string;
  [key: string]: unknown;
}

export interface SavedLayout {
  grid?: SerializedGrid;
  panels?: Record<string, unknown>;
  activeGroup?: string;
  [key: string]: unknown;
}

function isLeaf(node: GridNode): boolean {
  return node.type === "leaf";
}

function leafViews(node: GridNode): string[] {
  const data = node.data as { views?: string[] } | undefined;
  return Array.isArray(data?.views) ? data!.views! : [];
}

function branchChildren(node: GridNode): GridNode[] {
  return Array.isArray(node.data) ? (node.data as GridNode[]) : [];
}

/** Every panel id a serialized grid places, in document order. */
export function panelsInGrid(grid: SerializedGrid | undefined): string[] {
  const found: string[] = [];
  const walk = (node: GridNode | undefined) => {
    if (!node) return;
    if (isLeaf(node)) {
      found.push(...leafViews(node));
      return;
    }
    branchChildren(node).forEach(walk);
  };
  walk(grid?.root);
  return found;
}

/**
 * Drop panels that are no longer declared, and prune what that empties.
 *
 * Returns null when nothing is left — a branch with no children and a
 * leaf with no views are both meaningless to dockview, and a grid whose
 * root is one of those cannot be restored.
 */
function pruneNode(node: GridNode, declared: Set<string>): GridNode | null {
  if (isLeaf(node)) {
    const views = leafViews(node).filter((id) => declared.has(id));
    if (views.length === 0) return null;

    const data = (node.data ?? {}) as { activeView?: string };
    const activeView =
      data.activeView && views.includes(data.activeView)
        ? data.activeView
        : views[0];
    return { ...node, data: { ...data, views, activeView } };
  }

  const children = branchChildren(node)
    .map((child) => pruneNode(child, declared))
    .filter((child): child is GridNode => child !== null);

  if (children.length === 0) return null;
  // A branch reduced to one child is a redundant nesting level; dockview
  // accepts it, but collapsing keeps the tree the shape it would have
  // been declared as.
  if (children.length === 1) return children[0];
  return { ...node, data: children };
}

/**
 * Append panels the saved layout has never seen.
 *
 * They go into the LAST leaf, which is the least disruptive place that
 * is guaranteed to exist: a new panel becomes a tab beside something
 * rather than resizing the arrangement somebody set. Where the DSL puts
 * it is honoured only when nothing was saved at all, in which case the
 * declared default is used whole.
 */
function appendMissing(node: GridNode, missing: string[]): boolean {
  if (missing.length === 0) return true;

  let target: GridNode | null = null;
  const walk = (current: GridNode) => {
    if (isLeaf(current)) {
      target = current;
      return;
    }
    branchChildren(current).forEach(walk);
  };
  walk(node);

  if (!target) return false;
  const leaf = target as GridNode;
  const data = (leaf.data ?? {}) as { views?: string[] };
  data.views = [...(data.views ?? []), ...missing];
  leaf.data = data;
  return true;
}

/**
 * A saved layout made consistent with the declared panel set.
 *
 * `fallback` is used whole when the saved layout has nothing usable
 * left — a workspace rewritten from scratch should look like what its
 * author declared, not like a fragment of what it used to be.
 */
export function reconcileLayout(
  saved: SavedLayout | null | undefined,
  declaredPanelIds: string[],
  fallback: SavedLayout,
): SavedLayout {
  if (!saved?.grid?.root) return fallback;

  const declared = new Set(declaredPanelIds);
  const pruned = pruneNode(saved.grid.root, declared);
  if (!pruned) return fallback;

  const kept = new Set(panelsInGrid({ ...saved.grid, root: pruned }));
  const missing = declaredPanelIds.filter((id) => !kept.has(id));

  const root = JSON.parse(JSON.stringify(pruned)) as GridNode;
  if (!appendMissing(root, missing)) return fallback;

  // Per-panel state comes from the FRESH build, never from the save.
  //
  // A saved layout has been through JSON.stringify, the server, and
  // back, and dockview serializes each panel's `params` — which here
  // hold live callbacks (onMeasure, onToggleAuthorTools) and the
  // caller's current permissions. None of that survives a round trip,
  // so reusing saved params gives panels with dead callbacks and stale
  // rights.
  //
  // The saved layout contributes ARRANGEMENT — which group a panel is
  // in, and how big — and that lives in the grid above, not here.
  const panels: Record<string, unknown> = {};
  const savedPanels = (saved.panels ?? {}) as Record<string, unknown>;
  const fallbackPanels = (fallback.panels ?? {}) as Record<string, unknown>;
  for (const id of declaredPanelIds) {
    panels[id] = fallbackPanels[id] ?? savedPanels[id];
  }

  return { ...saved, grid: { ...saved.grid, root }, panels };
}

/**
 * The band a width falls into, as its lower bound.
 *
 * `breakpoints` are the declared minimums; the band starting at 0 is
 * implicit. A width below every declared breakpoint is band 0.
 */
export function bandFor(width: number, breakpoints: number[]): number {
  let band = 0;
  for (const min of [...breakpoints].sort((a, b) => a - b)) {
    if (width >= min) band = min;
  }
  return band;
}


/**
 * A stand-in width for the base band, which has no floor of its own.
 *
 * Every other band previews at its minimum, because that is where a
 * layout is tightest and therefore where it breaks. Band 0's minimum is
 * 0, so a phone width stands in for it.
 */
export const BASE_BAND_PREVIEW = 360;

/**
 * The width to constrain the canvas to while arranging `band`.
 *
 * An author on a 1500px screen arranging the 900 band is otherwise
 * guessing: they see it at 1500 and it is used at 900. Previewing at
 * the band's FLOOR rather than anywhere inside it is deliberate — a
 * layout that works at the floor works everywhere above it, and one
 * arranged at the top of a band can fall apart at the bottom.
 */
export function previewWidthFor(band: number): number {
  return band > 0 ? band : BASE_BAND_PREVIEW;
}

/**
 * How a band reads in a switcher.
 *
 * The upper bound comes from the next declared breakpoint, so the label
 * says what the band actually covers rather than just where it starts.
 */
export function bandLabel(band: number, breakpoints: number[]): string {
  const sorted = [...breakpoints].sort((a, b) => a - b);
  const next = sorted.find((b) => b > band);
  if (band === 0) return next ? `up to ${next - 1}px` : "all widths";
  return next ? `${band}\u2013${next - 1}px` : `${band}px and up`;
}


/**
 * What frontflow stores for one arrangement.
 *
 * dockview's own serialization is not quite enough. Collapsing a group
 * is frontflow's mechanism, not dockview's — it sets constraints, a
 * class and hidden overlays — so a restored grid brings back a
 * collapsed group's SIZE while leaving it uncollapsed. That is the
 * 31px strip of visible content this project has hit before, so which
 * groups were collapsed is recorded beside the grid.
 */
export interface StoredArrangement {
  dockview: SavedLayout;
  /** Ids of groups that were collapsed when this was saved. */
  collapsedGroups?: string[];
}

/**
 * Read a stored arrangement, accepting either shape.
 *
 * Rows written before collapse state was recorded hold a bare dockview
 * layout. Treating those as "no collapsed groups" is right — nothing
 * was recorded, so nothing is re-collapsed.
 */
export function unwrapStored(
  stored: unknown,
): { layout: SavedLayout | null; collapsedGroups: string[] } {
  if (!stored || typeof stored !== "object") {
    return { layout: null, collapsedGroups: [] };
  }
  const wrapped = stored as StoredArrangement & SavedLayout;
  if (wrapped.dockview && typeof wrapped.dockview === "object") {
    return {
      layout: wrapped.dockview,
      collapsedGroups: Array.isArray(wrapped.collapsedGroups)
        ? wrapped.collapsedGroups.filter((id) => typeof id === "string")
        : [],
    };
  }
  // A bare dockview layout, from before this wrapper existed.
  return { layout: wrapped as SavedLayout, collapsedGroups: [] };
}

/** Every group id a serialized grid names, so collapse can be matched. */
export function groupIdsInGrid(grid: SerializedGrid | undefined): string[] {
  const found: string[] = [];
  const walk = (node: GridNode | undefined) => {
    if (!node) return;
    if (isLeaf(node)) {
      const id = (node.data as { id?: string } | undefined)?.id;
      if (id) found.push(id);
      return;
    }
    branchChildren(node).forEach(walk);
  };
  walk(grid?.root);
  return found;
}
