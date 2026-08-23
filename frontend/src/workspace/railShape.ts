/**
 * Which inline styles turn a collapsed group's tab strip into a vertical
 * rail — computed as data, so the walk can be tested without a browser.
 *
 * Done inline rather than in CSS, which is not the obvious choice: the
 * elements between the tab and the rail belong to dockview and are
 * styled by dockview's own stylesheet, and rules aimed at them have lost
 * the cascade three separate ways here — to source order at equal
 * specificity, to Tailwind tree-shaking a layered rule whose class
 * appears in no source file, and to `.dv-single-tab.dv-full-width-single-tab`
 * being more specific than anything sensible to write.
 *
 * It walks EVERY ancestor up to the group rather than naming the three
 * it knows about. Centring only means something if the whole chain spans
 * the rail; one wrapper left at its natural width and the handle centres
 * inside that instead, which looks exactly like no centring at all.
 */

export interface StyleMutation {
  element: HTMLElement;
  property: string;
  value: string;
}

/** The class that ends the walk — the group the tab strip belongs to. */
export const GROUP_CLASS = "dv-groupview";

/**
 * A backstop above the group.
 *
 * Mid-drag a tab can be detached from any group, and without this the
 * walk climbs past where a group would have been and stretches `body`
 * and `html` to 100% — sizing the document to shape a tab strip.
 */
function isDocumentLevel(node: HTMLElement): boolean {
  return node.tagName === "BODY" || node.tagName === "HTML";
}

/**
 * Every inline style needed to shape `own`'s ancestry into a rail.
 *
 * Returns them rather than applying them so the decision is separable
 * from the DOM write, and so a test can assert on the whole set.
 */
export function railShapeMutations(own: HTMLElement): StyleMutation[] {
  const out: StyleMutation[] = [];
  const set = (element: HTMLElement, property: string, value: string) =>
    out.push({ element, property, value });

  set(own, "width", "100%");

  for (
    let node = own.parentElement;
    node && !node.classList.contains(GROUP_CLASS) && !isDocumentLevel(node);
    node = node.parentElement
  ) {
    set(node, "width", "100%");

    if (node.classList.contains("dv-tab")) {
      set(node, "padding", "0");
      // dockview gives a tab `min-width: 75px` — sensible for a
      // horizontal strip, and wider than the rail it now lives in, so
      // the tab overflowed and the handle centred in 75px rather than
      // in the 44px on screen.
      set(node, "min-width", "0");
    }
    if (node.classList.contains("dv-tabs-container")) {
      set(node, "flex-direction", "column");
      // Overflow meant for a one-line strip clips a taller handle.
      set(node, "overflow", "visible");
      set(node, "flex-grow", "0");
    }
    if (node.classList.contains("dv-tabs-and-actions-container")) {
      // The strip becomes the whole rail, not a one-line header.
      set(node, "height", "100%");
      set(node, "flex-direction", "column");
      set(node, "align-items", "stretch");
      break;
    }
  }

  return out;
}

/**
 * The nearest group above `own`, which is the subtree worth watching.
 *
 * A tab dropped into an already-collapsed rail mounts with the rail
 * already active, so the shaping effect runs once — and dockview
 * re-parents the tab as part of the drop, after that run. The styles
 * land on the ancestors the tab HAD, and its new ancestors never get
 * them, which is why such a tab sits uncentred until an
 * expand/collapse re-runs the walk against the right chain.
 */
export function groupOf(own: HTMLElement): HTMLElement | null {
  for (
    let node: HTMLElement | null = own.parentElement;
    node;
    node = node.parentElement
  ) {
    if (node.classList.contains(GROUP_CLASS)) return node;
  }
  return null;
}


// --- Shared ownership of the ancestors ------------------------------------
//
// Every tab in a collapsed group walks the SAME ancestors — the tab
// strip and its containers are shared by all of them. If each tab
// records "the value before I changed it" and restores that on expand,
// the second tab records the FIRST tab's value, and restoring it puts
// the rail styling straight back. One tab in a rail worked; a rail with
// several tabs dragged into it came back from an expand still shaped
// like a rail, with its strip stacked and its content mis-sized.
//
// So the original is recorded once, by whoever gets there first, and
// put back once, when the last owner lets go.

interface Owned {
  /** The inline value before any owner touched this property. */
  original: string;
  owners: Set<object>;
}

const owned = new WeakMap<HTMLElement, Map<string, Owned>>();
const claims = new WeakMap<object, Array<[HTMLElement, string]>>();

/** Apply `mutations` on behalf of `owner`, releasing anything it held. */
export function applyRailShape(
  owner: object,
  mutations: StyleMutation[],
): void {
  releaseRailShape(owner);

  const held: Array<[HTMLElement, string]> = [];
  for (const { element, property, value } of mutations) {
    let properties = owned.get(element);
    if (!properties) {
      properties = new Map();
      owned.set(element, properties);
    }
    let record = properties.get(property);
    if (!record) {
      record = {
        original: element.style.getPropertyValue(property),
        owners: new Set(),
      };
      properties.set(property, record);
    }
    record.owners.add(owner);
    held.push([element, property]);
    element.style.setProperty(property, value);
  }
  claims.set(owner, held);
}

/** Give up everything `owner` holds, restoring what nobody else wants. */
export function releaseRailShape(owner: object): void {
  const held = claims.get(owner);
  if (!held) return;
  claims.delete(owner);

  for (const [element, property] of held) {
    const properties = owned.get(element);
    const record = properties?.get(property);
    if (!record) continue;

    record.owners.delete(owner);
    if (record.owners.size > 0) continue;

    // Last one out puts it back.
    if (record.original) {
      element.style.setProperty(property, record.original);
    } else {
      element.style.removeProperty(property);
    }
    properties!.delete(property);
  }
}
