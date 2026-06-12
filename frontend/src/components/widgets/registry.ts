import { type Widget } from "./types";
import { distributionFilterWidget } from "./DistributionFilterWidget";
import { redistributionEditorWidget } from "./RedistributionEditorWidget";

/**
 * Widget registry. Maps the `widget` identifier in a HitlField to the
 * Widget bundle that handles it.
 *
 * Adding a new widget:
 *   1. Create a new component file in this directory.
 *   2. Export a `Widget<TValue>` const from it.
 *   3. Import and add an entry here.
 *
 * Both steps in one PR; the widget is fully self-contained.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export const widgetRegistry: Record<string, Widget<any>> = {
  distribution_filter: distributionFilterWidget,
  redistribution_editor: redistributionEditorWidget,
};

export function getWidget(name: string): Widget | undefined {
  return widgetRegistry[name];
}

export type { Widget, WidgetProps } from "./types";
export { BaseWidget } from "./BaseWidget";
