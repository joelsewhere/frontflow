# Widgets

Widgets are rich form inputs used inside HITL nodes. They produce
structured values, can do async work on submission (uploads, API
calls), and own their own display logic for the post-submit summary.

## File layout

```
widgets/
├── types.ts                 the Widget<TValue> interface + WidgetProps
├── BaseWidget.tsx           the shared field-shell every widget composes
├── registry.ts              maps widget name → Widget bundle
├── DistributionFilterWidget.tsx  reference implementation
└── README.md                this file
```

## The Widget contract

Every widget exports a `Widget<TValue>` const:

```ts
export interface Widget<TValue> {
  Component:        ComponentType<WidgetProps<TValue>>;
  renderSubmitted:  (value: TValue, field: HitlField) => ReactNode;
  validate?:        (value: TValue | undefined, field: HitlField) => string | null;
  beforeSubmit?:    (value: TValue | undefined, field: HitlField) => Promise<TValue | undefined>;
}
```

| Field | Required | Purpose |
| --- | --- | --- |
| `Component` | yes | The interactive React component the user interacts with. |
| `renderSubmitted` | yes | How to display the value in the post-submit summary. Return any ReactNode. |
| `validate` | no | Client-side validation. Returns `null` when valid, else an error message string. |
| `beforeSubmit` | no | Async hook to run after validation, before the form's onSubmit. Can transform the value (e.g. swap a file blob for an S3 key) and can throw to abort submission. |

`WidgetProps<TValue>` is what the `Component` receives:

```ts
export interface WidgetProps<TValue> {
  field:    HitlField;                  // the schema entry
  xcom:     Record<string, unknown>;    // upstream task XCom payload
  value:    TValue | undefined;         // current form value
  onChange: (value: TValue) => void;    // update form state
  error?:   string;                     // validation error
}
```

## Adding a new widget

Two-step process. Both go in one PR.

### 1. Create the widget file

```tsx
// widgets/MyWidget.tsx
import { BaseWidget } from "./BaseWidget";
import { type Widget, type WidgetProps } from "./types";

interface MyValue {
  /* whatever shape your widget produces */
}

function MyWidgetComponent({
  field, xcom, value, onChange, error
}: WidgetProps<MyValue>) {
  return (
    <BaseWidget label={field.label} error={error} hint="…">
      {/* your UI here, calling onChange(newValue) when the user
          interacts */}
    </BaseWidget>
  );
}

export const myWidget: Widget<MyValue> = {
  Component: MyWidgetComponent,
  renderSubmitted: (value) => /* JSX or string */,
  validate: (value, field) => {
    if (field.required && !value) return `${field.label} is required`;
    return null;
  },
  // beforeSubmit only if you need async work:
  // beforeSubmit: async (value, field) => { ... return newValue; }
};
```

### 2. Register it

```ts
// widgets/registry.ts
import { myWidget } from "./MyWidget";

export const widgetRegistry: Record<string, Widget<any>> = {
  distribution_filter: distributionFilterWidget,
  my_widget: myWidget,                       // <- add this
};
```

Then on the backend, set `widget: "my_widget"` on a `HitlField` of
type `"widget"`. The form will render your component.

## The `beforeSubmit` lifecycle

When the user clicks Continue, the form runs three sequential phases:

1. **Zod schema validation.** Standard scalar checks plus required-ness
   for widgets. Type mismatches and missing required fields surface
   here. Cheap, sync.
2. **Per-widget `validate()`.** Each widget's custom validation runs
   after Zod passes. Errors set via react-hook-form's `setError`.
3. **Per-widget `beforeSubmit()`.** All widget hooks run in parallel
   via `Promise.all`. Each may return a new value (which replaces what
   was in form state for the rest of the submission). Throwing
   aborts. The submit button shows "Preparing…" throughout.

Only after all three pass does the form call `onSubmit(values)` with
the final transformed values, which the parent uses for the API call
to `/hitl/{run}/{task}`.

## Use cases for `beforeSubmit`

- **File upload widget.** During editing, `value` is `{ file: File, preview }`.
  On submit, upload the file and return `{ s3_key, row_count }`. The
  Airflow task receives the s3_key, not the blob.
- **Geocoding widget.** Editing produces a free-text address; on submit,
  geocode it and return `{ address, lat, lng }`.
- **API-validated widget.** Editing produces a string; on submit, hit a
  backend endpoint to validate (e.g., "is this account ID known?") and
  either return enriched data or throw with a meaningful error.

## Visualizations vs. widgets

Widgets are form inputs. Standalone visualizations (read-only charts,
descriptive stats, tables) belong in `components/charts/`. The
`<DistributionFilterWidget>` is a widget; it composes the
`<Histogram>` chart from `charts/` to render its bars. Same chart
primitive works in non-widget contexts:

```tsx
// Inside a non-interactive report node:
<Histogram data={weeklyCounts} height={200} />
<DescriptiveStats values={[1, 3, 7, 12, ...]} />
```

See `components/charts/README.md` for chart-side conventions.
