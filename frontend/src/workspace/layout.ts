/**
 * The declared panel tree, turned into a dockview grid.
 *
 * This is the part with real structure in it, so it lives on its own
 * and stays pure: tree in, serialized layout out, no dockview instance
 * and no DOM. It can be run and checked directly.
 *
 * Why a serialized layout rather than a sequence of `addPanel` calls:
 * `addPanel` places a panel relative to ONE other panel, splitting that
 * panel's group. There is no way to say "below this entire row" — the
 * best you can do is pick a panel inside the row, and the new panel
 * lands under that one column. Anything deeper than a single split
 * therefore comes out wrong, which is exactly what happened when a
 * workspace first nested a Row inside a Column.
 *
 * The one rule that shapes everything here: **dockview alternates
 * orientation with depth.** A branch's children are always laid out
 * perpendicular to it (`orthogonal(orientation)` in its deserializer),
 * and there is no per-branch orientation in the format. So a Column
 * directly inside a Column cannot be represented — it has to be
 * flattened into its parent first, which is what `normalize` does.
 */

export type Orientation = "HORIZONTAL" | "VERTICAL";

export interface LayoutBlock {
  type: string;
  id: string | null;
  props: Record<string, unknown>;
  children: LayoutBlock[];
}

/** Panel kinds a workspace may contain. Anything else is a container. */
export const PANEL_TYPES = new Set([
  "workspace_form",
  "dashboard",
  "superset_explore",
]);

/** A group of panels sharing one region — one dock group, tabbed. */
export interface LeafNode {
  type: "leaf";
  panels: LayoutBlock[];
}

export interface BranchNode {
  type: "branch";
  orientation: Orientation;
  children: NormalizedNode[];
}

export type NormalizedNode = LeafNode | BranchNode;

/**
 * The declared tree, with same-orientation nesting flattened away.
 *
 * A Row inside a Row (or Column in Column) is meaningless in a grid
 * that alternates orientation by depth, so its children are spliced
 * into the parent. After this, any branch child of a branch is
 * guaranteed to be the opposite orientation — which is precisely what
 * the serialized format assumes.
 */
export function normalize(block: LayoutBlock): NormalizedNode {
  if (PANEL_TYPES.has(block.type)) {
    return { type: "leaf", panels: [block] };
  }

  if (block.type === "tabs") {
    // Tabs share one region, so they are one leaf however they nest.
    return { type: "leaf", panels: panelsUnder(block) };
  }

  const orientation: Orientation =
    block.type === "row" ? "HORIZONTAL" : "VERTICAL";

  const children = (block.children ?? [])
    .map(normalize)
    .filter((child) => child.type === "branch" || child.panels.length > 0)
    .flatMap((child) =>
      child.type === "branch" && child.orientation === orientation
        ? child.children
        : [child],
    );

  // A container holding one thing is not a split; collapsing it keeps
  // the depth honest, and depth is what decides orientation.
  if (children.length === 1) return children[0];
  return { type: "branch", orientation, children };
}

function panelsUnder(block: LayoutBlock): LayoutBlock[] {
  if (PANEL_TYPES.has(block.type)) return [block];
  return (block.children ?? []).flatMap(panelsUnder);
}

export interface Box {
  width: number;
  height: number;
}

export interface SerializedNode {
  type: "leaf" | "branch";
  size: number;
  data: SerializedNode[] | { id: string; views: string[]; activeView?: string };
}

export interface DockLayout {
  grid: {
    root: SerializedNode;
    width: number;
    height: number;
    orientation: Orientation;
  };
  panels: Record<string, unknown>;
}

/** The dock panel id for a declared block. */
export function panelId(block: LayoutBlock, fallbackIndex: number): string {
  return block.id ?? `${block.type}-${fallbackIndex}`;
}

/**
 * How much of its parent's axis a node should claim.
 *
 * Along a Column, a panel's declared or measured height is a real
 * statement about how much room it needs, so it is honoured
 * proportionally. Across a Row there is no such signal, so siblings
 * share equally.
 */
function weight(node: NormalizedNode, heights: Record<string, number>): number {
  if (node.type === "leaf") {
    return Math.max(
      1,
      ...node.panels.map((panel, index) => {
        const declared = (panel.props.min_height as number | null) ?? 0;
        return Math.max(heights[panelId(panel, index)] ?? 0, declared, 1);
      }),
    );
  }
  if (node.orientation === "VERTICAL") {
    return node.children.reduce((sum, c) => sum + weight(c, heights), 0);
  }
  return Math.max(...node.children.map((c) => weight(c, heights)));
}

function distribute(total: number, weights: number[]): number[] {
  const sum = weights.reduce((a, b) => a + b, 0);
  if (sum <= 0) return weights.map(() => Math.floor(total / weights.length));
  const sizes = weights.map((w) => Math.max(1, Math.floor((total * w) / sum)));
  // Give the rounding remainder to the last child so the parts add up.
  const drift = total - sizes.reduce((a, b) => a + b, 0);
  sizes[sizes.length - 1] += drift;
  return sizes;
}

function serializeNode(
  node: NormalizedNode,
  box: Box,
  parentOrientation: Orientation,
  heights: Record<string, number>,
  counter: { next: number },
): SerializedNode {
  // `size` is always the node's extent along its PARENT's axis.
  const size =
    parentOrientation === "HORIZONTAL" ? box.width : box.height;

  if (node.type === "leaf") {
    const views = node.panels.map((panel) => panelId(panel, counter.next++));
    return {
      type: "leaf",
      size,
      data: { id: `group-${views[0]}`, views, activeView: views[0] },
    };
  }

  const along = node.orientation === "HORIZONTAL" ? box.width : box.height;
  const sizes = distribute(
    along,
    node.children.map((child) => weight(child, heights)),
  );

  return {
    type: "branch",
    size,
    data: node.children.map((child, index) =>
      serializeNode(
        child,
        node.orientation === "HORIZONTAL"
          ? { width: sizes[index], height: box.height }
          : { width: box.width, height: sizes[index] },
        node.orientation,
        heights,
        counter,
      ),
    ),
  };
}

/**
 * The declared tree as a dockview layout.
 *
 * `panelState` supplies each panel's dockview state (component, title,
 * params); it is called once per panel in layout order.
 */
export function buildDockLayout(
  block: LayoutBlock,
  box: Box,
  panelState: (block: LayoutBlock, id: string) => unknown,
  heights: Record<string, number> = {},
): DockLayout {
  const root = normalize(block);
  const orientation: Orientation =
    root.type === "branch" ? root.orientation : "VERTICAL";

  const panels: Record<string, unknown> = {};
  const collect = (node: NormalizedNode, counter: { next: number }) => {
    if (node.type === "leaf") {
      for (const panel of node.panels) {
        const id = panelId(panel, counter.next++);
        panels[id] = panelState(panel, id);
      }
      return;
    }
    for (const child of node.children) collect(child, counter);
  };

  // Two walks share one counter sequence, so a block with no declared
  // id gets the same fallback in both.
  collect(root, { next: 0 });
  const serialized = serializeNode(
    root,
    box,
    orientation,
    heights,
    { next: 0 },
  );

  return {
    grid: {
      root: serialized,
      width: box.width,
      height: box.height,
      orientation,
    },
    panels,
  };
}
