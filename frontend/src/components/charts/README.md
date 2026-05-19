# Charts

Standard data visualizations and statistical summaries. Built on
[Visx](https://airbnb.io/visx/) — Airbnb's React-native primitives
over D3 — with theme tokens for all colors and typography.

These components are usable in any context: read-only report nodes,
dashboards, or as building blocks for widgets that need interactivity
(see `../widgets/`).

## When to use what

| Use case | Component |
| --- | --- |
| Value over time (KPIs, run frequencies, daily counts) | `<TimeSeriesChart>` |
| Category comparison (totals per type, counts per region) | `<BarChart>` |
| Distribution of counts over ordered buckets | `<Histogram>` |
| Numeric summary (mean, median, min, max, n) | `<DescriptiveStats>` |
| Distribution with user-selected range | `<DistributionFilterWidget>` *(in `widgets/`)* |
| Anything else | Compose from Visx primitives — see "Adding a new chart" |

## Examples

```tsx
import {
  TimeSeriesChart,
  BarChart,
  Histogram,
  DescriptiveStats,
} from "@/components/charts";

// Time series
<TimeSeriesChart
  data={[{ date: "2025-01-06", value: 12 }, /* ... */]}
  yLabel="Records / week"
/>

// Categorical bars
<BarChart
  data={[
    { label: "Residential", value: 42 },
    { label: "Commercial",  value: 18 },
    { label: "Mixed-use",   value: 9 },
  ]}
  yLabel="Properties"
/>

// Histogram
<Histogram
  data={[
    { label: "2025-01-06", count: 3 },
    { label: "2025-01-13", count: 5 },
    /* ... */
  ]}
  height={220}
/>

// Descriptive stats
<DescriptiveStats
  values={[3, 5, 4, 7, 12, 18, 22, 31]}
  stats={["count", "mean", "median", "max"]}
/>
```

All charts are responsive. Heights are props with sensible defaults;
widths are inferred from the parent container via `ResizeObserver`.

## Conventions

### Colors

Always use CSS variables from the theme:

| Variable | Use for |
| --- | --- |
| `rgb(var(--color-accent))` | Primary data series |
| `rgb(var(--color-ink))`    | Secondary series, dark fills |
| `rgb(var(--color-muted))`  | Axis lines, tick labels |
| `rgb(var(--color-border))` | Grid lines |
| `rgb(var(--color-surface))`| Backgrounds |

Never hardcode hex colors.

### Typography

Axis labels and tick text:

```ts
fontFamily: "var(--font-mono)"
fontSize: 10
fill: "rgb(var(--color-muted))"
```

Use the `axisLabelProps()` helper from `./conventions.ts`.

### Layout

Margin-based. The SVG is the full width × height; the chart drawing
area is inset by margins for axes. Use `DEFAULT_MARGIN` unless axes
need more room.

### Responsive width

Use the `useChartWidth()` hook:

```tsx
import { useChartWidth } from "./conventions";

const [containerRef, width] = useChartWidth();
// width updates via ResizeObserver as the container resizes
```

## Composition with widgets

Charts in this directory are inert (no selection, no interaction
beyond basic tooltips). Widgets in `../widgets/` compose them and add
the interactive bits.

The `<Histogram>` component supports two render-prop slots so a widget
can overlay content without re-implementing bars + axis:

```tsx
<Histogram
  data={data}
  height={200}
  // Bars are dimmed outside the widget's selection
  barOpacity={(_, i) => isInSelection(i) ? 0.85 : 0.2}
  disableTooltip               // widget provides its own
  svgRef={svgRef}              // for pointer event coordination
  svgProps={{ onPointerMove, onPointerUp }}
  renderOverlay={(scales, { chartH, vertPad }) => (
    <>
      {/* SVG content layered between bars and axis */}
      <SelectionRect ... />
      <Handles ... />
    </>
  )}
  renderHtmlOverlay={({ scales }) => (
    /* HTML positioned over the chart container */
    <Tooltip ... />
  )}
/>
```

The `useHistogramScales` hook (exported from `./conventions`) lets a
widget compute matching scales for pointer hit-testing or any other
math, so the widget's overlay coordinates stay aligned with the bars.

`DistributionFilterWidget` is the reference for this pattern.

## Adding a new chart

1. Create a new file (e.g. `ScatterChart.tsx`).
2. Use Visx primitives: `@visx/scale`, `@visx/axis`, `@visx/shape`.
   Add new sub-packages only if a chart genuinely needs them — keep
   the bundle pay-as-you-go.
3. Use `DEFAULT_MARGIN`, `axisLabelProps()`, and `useChartWidth()`
   for consistency.
4. All colors via CSS variables.
5. Export from `index.ts`.
6. Add a row to "When to use what" above.

For charts whose interactivity doesn't have a clean Visx primitive,
expose render-prop slots like `<Histogram>` does, or drop down to
plain SVG + pointer events. Both approaches coexist in the same
component — see `widgets/DistributionFilterWidget.tsx`.

## Why Visx

See the discussion in the top-level project README. Short version:
composable React-native primitives, tree-shakeable, built on D3
underneath. Plays well with custom interactions when standard chart
APIs hit walls.
