/**
 * Recursive layout-tree renderer. Dispatches each block to a themed
 * React component by its `type` (a Dash-style component registry).
 *
 * Two render modes, carried via BlockRenderContext:
 *   - "form":      input blocks are editable (react-hook-form); button
 *                  blocks submit.
 *   - "submitted": input blocks show their value read-only; the clicked
 *                  button shows as chosen.
 */

import {
  createContext,
  useContext,
  useState,
  type ComponentType,
  type ReactNode,
} from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeRaw from "rehype-raw";
import { Controller, useFormContext, useWatch } from "react-hook-form";
import { type Block } from "../../lib/api";
import {
  CommentThread,
  CommentToggle,
} from "../comments/CommentThread";
import { useBlockRender, useNodeForm } from "./types";
import {
  synthWidgetField,
  synthRedistributionField,
  synthCategorizerField,
  FIELD_TYPES,
} from "./schema";
import {
  CLICKED_BUTTON_KEY,
  evalConditions,
  interpolate,
  readConditions,
} from "./conditions";
import { TextField } from "../forms/TextField";
import { Field } from "../forms/Field";
import { FileUploadField } from "../forms/FileUploadField";
import { SankeyField, type SankeyLink } from "../forms/SankeyField";
import { NumberField } from "../forms/NumberField";
import { SelectField } from "../forms/SelectField";
import { TextareaField } from "../forms/TextareaField";
import { CheckboxField } from "../forms/CheckboxField";
import { RadioField } from "../forms/RadioField";
import { DateRangeField } from "../forms/DateRangeField";
import { NumberRangeField } from "../forms/NumberRangeField";
import { MultiSelectField } from "../forms/MultiSelectField";
import { CheckboxGridField } from "../forms/CheckboxGridField";
import { CheckboxListField } from "../forms/CheckboxListField";
import { getWidget } from "../widgets/registry";
import { DashboardEmbed } from "./DashboardBlock";
import { usePickerOptions } from "../../hooks/usePickerOptions";

interface BlockProps {
  block: Block;
}

/** True while rendering inside a Row. A button inside a Row fills its
 *  slot, so a row of buttons is uniform in width (and, being single-
 *  line, in height). */
const RowContext = createContext(false);

/** Render a block's children, recursing through BlockTree. */
function Children({ block }: BlockProps) {
  return (
    <>
      {block.children.map((c, i) => (
        <BlockTree key={c.id ?? `${c.type}-${i}`} block={c} />
      ))}
    </>
  );
}

// --- Containers ------------------------------------------------------------

function ColumnBlock({ block }: BlockProps) {
  return (
    <div className="flex flex-col gap-5">
      <Children block={block} />
    </div>
  );
}

function RowBlock({ block }: BlockProps) {
  return (
    <RowContext.Provider value={true}>
      <div className="flex flex-col sm:flex-row gap-4">
        {block.children.map((c, i) => (
          <div key={c.id ?? `${c.type}-${i}`} className="min-w-0 flex-1">
            <BlockTree block={c} />
          </div>
        ))}
      </div>
    </RowContext.Provider>
  );
}

function CardBlock({ block }: BlockProps) {
  const title = block.props.title as string | undefined;
  return (
    <div className="border border-border bg-surface p-5 flex flex-col gap-4">
      {title ? (
        <p className="text-[10px] uppercase tracking-[0.2em] text-muted font-mono">
          {title}
        </p>
      ) : null}
      <Children block={block} />
    </div>
  );
}

function SectionBlock({ block }: BlockProps) {
  const title = block.props.title as string | undefined;
  return (
    <section className="flex flex-col gap-3">
      {title ? (
        <h3 className="font-display text-lg font-semibold text-ink">{title}</h3>
      ) : null}
      <Children block={block} />
    </section>
  );
}

const CALLOUT_STYLES: Record<string, string> = {
  info: "border-border bg-bg",
  success: "border-accent bg-accent/5",
  warning: "border-ink bg-surface",
  error: "border-error bg-error/5",
};

function CalloutBlock({ block }: BlockProps) {
  const variant = (block.props.variant as string) ?? "info";
  const style = CALLOUT_STYLES[variant] ?? CALLOUT_STYLES.info;
  return (
    <div className={`border-l-2 px-4 py-3 flex flex-col gap-2 ${style}`}>
      <Children block={block} />
    </div>
  );
}

/**
 * Shared shell for titled toggle sections: a full-width header (title
 * + chevron when there is a body), an always-visible summary region,
 * and a body that expands/collapses. `type="button"` matters — blocks
 * render inside the node's <form>, and a bare <button> would submit
 * it. With no body the header is a static bar (no chevron, no
 * toggle); with no summary the body is all there is (classic
 * collapsible).
 */
function CollapsibleShell({
  title,
  initialOpen,
  summary,
  children,
}: {
  title: string;
  initialOpen: boolean;
  summary?: ReactNode;
  children?: ReactNode;
}) {
  const [open, setOpen] = useState(initialOpen);
  const hasBody = children != null;
  const header = (
    <>
      <span className="text-[10px] uppercase tracking-[0.2em] text-muted font-mono">
        {title}
      </span>
      {hasBody ? (
        <span
          className={`text-muted text-xs transition-transform ${
            open ? "rotate-90" : ""
          }`}
        >
          ▸
        </span>
      ) : null}
    </>
  );
  return (
    <div className="border border-border bg-surface">
      {hasBody ? (
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          className="w-full flex items-center justify-between gap-3 px-5 py-3 text-left cursor-pointer"
        >
          {header}
        </button>
      ) : (
        <div className="w-full flex items-center justify-between gap-3 px-5 py-3">
          {header}
        </div>
      )}
      {summary ? (
        <div className="px-5 pb-5 flex flex-col gap-4">{summary}</div>
      ) : null}
      {hasBody && open ? (
        <div className="px-5 pb-5 flex flex-col gap-4">{children}</div>
      ) : null}
    </div>
  );
}

function CollapsibleBlock({ block }: BlockProps) {
  // A titled toggle group with an optional always-visible summary.
  // With `has_summary`, the compiler emitted exactly two children:
  // [0] the summary (rendered whether collapsed or not) and [1] a
  // column holding the body (rendered only when expanded). Without
  // it, every child is body. `props.open` picks the initial state
  // only; the user owns the toggle after that.
  const title = (block.props.title as string) ?? "";
  const hasSummary = Boolean(block.props.has_summary);
  const summaryBlock = hasSummary ? block.children[0] : null;
  const bodyBlocks = hasSummary ? block.children.slice(1) : block.children;
  const bodyIsEmpty =
    bodyBlocks.length === 0 ||
    (hasSummary && (bodyBlocks[0]?.children?.length ?? 0) === 0);
  return (
    <CollapsibleShell
      title={title}
      initialOpen={Boolean(block.props.open)}
      summary={
        summaryBlock ? <BlockTree block={summaryBlock} /> : undefined
      }
    >
      {bodyIsEmpty ? undefined : (
        <>
          {bodyBlocks.map((c, i) => (
            <BlockTree key={c.id ?? `${c.type}-${i}`} block={c} />
          ))}
        </>
      )}
    </CollapsibleShell>
  );
}

// --- Conditional container -------------------------------------------------

/**
 * A `When` block — its children render only while its conditions hold,
 * evaluated live against the form's current values. When hidden it
 * renders nothing, so the revealed fields flow inline with their
 * siblings once shown. Works in both form and submitted mode (in
 * submitted mode the values are the submitted ones).
 */
function WhenBlock({ block }: BlockProps) {
  const conditions = readConditions(block.props);
  const values = useWatch() as Record<string, unknown> | undefined;
  // Merge the clicked-button id into the values dict under the
  // sentinel key the `button_clicked` op reads. Pre-submit mode has
  // `clickedButton = null` → after-submit content stays hidden;
  // submitted mode supplies the recorded id → the matching button's
  // When block activates. The merged object is read-only for the
  // condition evaluation; we never mutate `values` itself.
  const ctx = useBlockRender();
  const augmented = {
    ...(values ?? {}),
    [CLICKED_BUTTON_KEY]: ctx.clickedButton,
  };
  if (!evalConditions(conditions, augmented)) return null;
  return <Children block={block} />;
}

// --- Display leaves --------------------------------------------------------

const MD_COMPONENTS: Components = {
  // Heading levels read their size/weight from the per-form theme.
  // Tags are de-escalated (a markdown `#` renders as <h2>) so form
  // content never competes with the page's own <h1>; the styling still
  // comes from the matching header level.
  h1: ({ children }) => (
    <h2
      className="font-display mt-1"
      style={{
        fontSize: "var(--h1-size)",
        fontWeight: "var(--h1-weight)",
        color: "rgb(var(--h1-color))",
      }}
    >
      {children}
    </h2>
  ),
  h2: ({ children }) => (
    <h3
      className="font-display mt-1"
      style={{
        fontSize: "var(--h2-size)",
        fontWeight: "var(--h2-weight)",
        color: "rgb(var(--h2-color))",
      }}
    >
      {children}
    </h3>
  ),
  h3: ({ children }) => (
    <h4
      className="font-display mt-1"
      style={{
        fontSize: "var(--h3-size)",
        fontWeight: "var(--h3-weight)",
        color: "rgb(var(--h3-color))",
      }}
    >
      {children}
    </h4>
  ),
  h4: ({ children }) => (
    <h5
      className="font-display mt-1"
      style={{
        fontSize: "var(--h4-size)",
        fontWeight: "var(--h4-weight)",
        color: "rgb(var(--h4-color))",
      }}
    >
      {children}
    </h5>
  ),
  p: ({ children }) => (
    <p className="text-sm text-muted leading-relaxed">{children}</p>
  ),
  ul: ({ children }) => (
    <ul className="list-disc pl-5 text-sm text-muted flex flex-col gap-1">
      {children}
    </ul>
  ),
  ol: ({ children }) => (
    <ol className="list-decimal pl-5 text-sm text-muted flex flex-col gap-1">
      {children}
    </ol>
  ),
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  a: ({ children, href }) => (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="text-ink underline underline-offset-2 hover:text-accent"
    >
      {children}
    </a>
  ),
  strong: ({ children }) => (
    <strong
      style={{
        fontWeight: "var(--bold-weight)",
        color: "rgb(var(--bold-color))",
      }}
    >
      {children}
    </strong>
  ),
  em: ({ children }) => (
    <em className="italic" style={{ color: "rgb(var(--italic-color))" }}>
      {children}
    </em>
  ),
  u: ({ children }) => (
    <u style={{ color: "rgb(var(--underline-color))" }}>{children}</u>
  ),
  code: ({ children }) => (
    <code className="font-mono text-xs bg-bg border border-border px-1 py-0.5">
      {children}
    </code>
  ),
  blockquote: ({ children }) => (
    <blockquote className="border-l-2 border-border pl-3 text-muted italic">
      {children}
    </blockquote>
  ),
  hr: () => <hr className="border-0 border-t border-border my-1" />,
  table: ({ children }) => (
    <table className="w-full text-sm border-collapse">{children}</table>
  ),
  th: ({ children }) => (
    <th className="text-left border-b border-border py-1 px-2 font-mono text-xs uppercase tracking-wider text-muted">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="border-b border-border py-1 px-2 text-ink">{children}</td>
  ),
};

function MarkdownBlock({ block }: BlockProps) {
  const source = (block.props.source as string) ?? "";
  // Cap prose at a comfortable reading width even when the parent
  // container is wide (e.g. inside a `@page` that fills the outer
  // 7xl). KPIs, Figures, tables, and other display blocks still get
  // the full container width — only Markdown self-constrains here,
  // since long-line prose at 1200+px reads poorly.
  return (
    <div className="flex flex-col gap-2 max-w-2xl">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeRaw]}
        components={MD_COMPONENTS}
      >
        {source}
      </ReactMarkdown>
    </div>
  );
}

function DividerBlock() {
  return <hr className="border-0 border-t border-border" />;
}

function ImageBlock({ block }: BlockProps) {
  const src = block.props.src as string;
  const alt = (block.props.alt as string) ?? "";
  const caption = block.props.caption as string | undefined;
  return (
    <figure className="flex flex-col gap-1.5">
      <img src={src} alt={alt} className="w-full border border-border" />
      {caption ? (
        <figcaption className="text-xs text-muted">{caption}</figcaption>
      ) : null}
    </figure>
  );
}

function KPIBlock({ block }: BlockProps) {
  // A label above, a large centered number below, bordered. Fills the
  // width its container gives it — drop two in a Row for a side-by-side
  // metric strip.
  const label = (block.props.label as string) ?? "";
  const value = (block.props.value as string) ?? "";
  const inRow = useContext(RowContext);
  return (
    <div
      className={[
        "flex flex-col items-center justify-center gap-2",
        "border border-border bg-surface px-4 py-6",
        inRow ? "w-full" : "",
      ].join(" ")}
    >
      <div className="text-xs uppercase tracking-wider text-muted">
        {label}
      </div>
      <div className="text-3xl font-semibold text-fg">{value}</div>
    </div>
  );
}

type BlobHandle = { kind?: string; hash?: string; content_type?: string };

/** One chart image resolved through the submission blob proxy — the
 * same URL scheme FigureBlock uses, for handles that arrived nested
 * inside a backend's dict return (KPIGroups group charts). */
function GroupChart({
  caption,
  handle,
}: {
  caption: string;
  handle: BlobHandle;
}) {
  const { formId, submissionId } = useBlockRender();
  if (!handle?.hash || !submissionId) return null;
  const url =
    `/api/forms/${encodeURIComponent(formId)}` +
    `/submissions/${encodeURIComponent(submissionId)}` +
    `/blob/${encodeURIComponent(handle.hash)}`;
  return (
    <figure className="flex flex-col gap-1.5 min-w-0 flex-1">
      <img
        src={url}
        alt={caption}
        style={{ maxWidth: "100%" }}
        className="border border-border"
      />
      <figcaption className="text-xs text-muted">{caption}</figcaption>
    </figure>
  );
}

function KPIGroupsBlock({ block }: BlockProps) {
  // Data-driven KPI sections: `data` is a dict keyed by group title —
  // typically a @backend's return resolved server-side via
  // `data_from`. Two group shapes:
  //   flat:       {kpi label: value}
  //   structured: {"kpis": {label: value}, "charts": {caption: blob}}
  // Either way the KPI strip renders in the always-visible summary
  // position. A structured group's charts become the expandable body
  // (chevron appears); a flat group renders as a static bar.
  const data =
    (block.props.data as Record<string, Record<string, unknown>>) ?? {};
  const initialOpen = Boolean(block.props.open);
  const groups = Object.entries(data);
  if (groups.length === 0) return null;
  return (
    <div className="flex flex-col gap-3">
      {groups.map(([title, group]) => {
        const structured =
          group != null &&
          typeof group === "object" &&
          typeof (group as Record<string, unknown>).kpis === "object";
        const kpis = (
          structured
            ? (group as Record<string, unknown>).kpis
            : group
        ) as Record<string, unknown>;
        const charts = (
          structured
            ? ((group as Record<string, unknown>).charts ?? {})
            : {}
        ) as Record<string, BlobHandle>;
        const chartEntries = Object.entries(charts).filter(
          ([, h]) => h?.hash,
        );
        const strip = (
          <div className="flex flex-col sm:flex-row gap-4">
            {Object.entries(kpis ?? {}).map(([label, value]) => (
              <div
                key={label}
                className="min-w-0 flex-1 flex flex-col items-center justify-center gap-2 border border-border bg-bg px-4 py-6"
              >
                <div className="text-xs uppercase tracking-wider text-muted">
                  {label}
                </div>
                <div className="text-3xl font-semibold text-fg">
                  {String(value)}
                </div>
              </div>
            ))}
          </div>
        );
        return (
          <CollapsibleShell
            key={title}
            title={title}
            initialOpen={initialOpen}
            summary={strip}
          >
            {chartEntries.length > 0 ? (
              <div className="flex flex-col sm:flex-row sm:flex-wrap gap-4">
                {chartEntries.map(([caption, handle]) => (
                  <GroupChart
                    key={caption}
                    caption={caption}
                    handle={handle}
                  />
                ))}
              </div>
            ) : undefined}
          </CollapsibleShell>
        );
      })}
    </div>
  );
}


function FigureBlock({ block }: BlockProps) {
  // The block's `data` prop is a blob handle the runtime built when
  // the `@backend` returned raw bytes: { kind, hash, content_type,
  // size }. We don't ship the bytes inline; instead the browser hits
  // the form server's blob proxy, which streams them with the right
  // Content-Type so an <img> just works.
  const { formId, submissionId } = useBlockRender();
  const data = block.props.data as
    | { kind?: string; hash?: string; content_type?: string }
    | undefined;
  const alt = (block.props.alt as string) ?? "";
  const caption = block.props.caption as string | undefined;
  const width = block.props.width as string | undefined;
  const height = block.props.height as string | undefined;

  // No handle (backend hasn't run, or returned non-bytes). Render
  // nothing rather than a broken image — the block resolves to a
  // real image once the upstream value lands.
  if (!data?.hash || !submissionId) {
    return null;
  }
  const url =
    `/api/forms/${encodeURIComponent(formId)}` +
    `/submissions/${encodeURIComponent(submissionId)}` +
    `/blob/${encodeURIComponent(data.hash)}`;

  // Default sizing: fill width, auto height — matches most "show me
  // a chart inline" cases. Author overrides via width/height kwargs.
  const style: Record<string, string> = {};
  if (width) style.width = width;
  if (height) style.height = height;
  if (!width && !height) style.maxWidth = "100%";

  return (
    <figure className="flex flex-col gap-1.5">
      <img
        src={url}
        alt={alt}
        style={style}
        // No bg-* class — the <img> sits on whatever surface its
        // parent layout provides, which is what's needed for the
        // common case where the backend wrote the PNG with
        // `transparent=True` and wants the page surface to show
        // through. Opaque PNGs paint their own background and look
        // identical with or without this class.
        className="border border-border"
      />
      {caption ? (
        <figcaption className="text-xs text-muted">{caption}</figcaption>
      ) : null}
    </figure>
  );
}


function S3DownloadBlock({ block }: BlockProps) {
  // The block carries `bucket` and the resolved `key`, but the URL the
  // user clicks is a proxy on the form server — it mints a fresh
  // presigned URL on every click, so the link survives indefinitely
  // even if the page sat open across the presigned URL's short TTL.
  // The `bucket` / `key` props themselves stay client-side as a hint;
  // they aren't used for navigation.
  const { formId, submissionId, nodeId } = useBlockRender();
  const label = (block.props.label as string) ?? "Download";
  const blockId = block.id;
  const inRow = useContext(RowContext);

  // No submission yet (landing screen) or no id (compile bug): render
  // disabled — there's nothing to fetch.
  if (!submissionId || !blockId) {
    return (
      <span
        className={[
          BUTTON_BASE,
          variantClass("secondary"),
          "no-underline opacity-50 cursor-not-allowed",
          inRow ? "w-full" : "",
        ].join(" ")}
        aria-disabled="true"
      >
        {label}
      </span>
    );
  }

  const url =
    `/api/forms/${encodeURIComponent(formId)}` +
    `/submissions/${encodeURIComponent(submissionId)}` +
    `/download/${encodeURIComponent(nodeId)}` +
    `/${encodeURIComponent(blockId)}`;

  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      className={[
        BUTTON_BASE,
        variantClass("secondary"),
        "no-underline inline-flex items-center gap-2",
        inRow ? "w-full justify-center" : "",
      ].join(" ")}
    >
      <svg
        viewBox="0 0 16 16"
        width="14"
        height="14"
        aria-hidden="true"
        className="shrink-0"
      >
        <path
          d="M8 2v8m0 0L4.5 6.5M8 10l3.5-3.5M3 13h10"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="square"
        />
      </svg>
      <span>{label}</span>
    </a>
  );
}


function TableBlock({ block }: BlockProps) {
  const title = block.props.title as string | null;
  const data = (block.props.data as Record<string, unknown>) ?? {};
  return (
    <div className="bg-bg border border-border px-4 py-3">
      {title ? (
        <p className="text-[10px] uppercase tracking-[0.2em] text-muted font-mono mb-2">
          {title}
        </p>
      ) : null}
      <dl className="grid grid-cols-1 gap-1.5">
        {Object.entries(data).map(([k, v]) => (
          <div
            key={k}
            className="flex justify-between gap-3 text-sm font-mono"
          >
            <dt className="text-muted">{k}</dt>
            <dd className="text-ink text-right">{String(v)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

// --- Submitted-value shell -------------------------------------------------

function SubmittedField({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-[10px] uppercase tracking-[0.2em] text-muted font-mono">
        {label}
      </span>
      <div className="font-sans text-base font-medium text-ink break-words">
        {children}
      </div>
    </div>
  );
}

// --- Input blocks ----------------------------------------------------------

function useFieldError(id: string): string | undefined {
  const {
    formState: { errors },
  } = useFormContext();
  return errors[id]?.message as string | undefined;
}

function TextInputBlock({ block }: BlockProps) {
  const { mode, values } = useBlockRender();
  const id = block.id ?? "";
  const label = (block.props.label as string) ?? id;
  if (mode === "submitted") {
    return (
      <SubmittedField label={label}>
        {formatScalar(values[id])}
      </SubmittedField>
    );
  }
  return <RHFText block={block} />;
}

function RHFText({ block }: BlockProps) {
  const { register } = useFormContext();
  const id = block.id ?? "";
  return (
    <TextField
      label={(block.props.label as string) ?? id}
      placeholder={(block.props.placeholder as string) || undefined}
      hint={(block.props.help as string) || undefined}
      error={useFieldError(id)}
      {...register(id)}
    />
  );
}

function NumberInputBlock({ block }: BlockProps) {
  const { mode, values } = useBlockRender();
  const id = block.id ?? "";
  const label = (block.props.label as string) ?? id;
  if (mode === "submitted") {
    return (
      <SubmittedField label={label}>
        {formatScalar(values[id])}
      </SubmittedField>
    );
  }
  return <RHFNumber block={block} />;
}

function RHFNumber({ block }: BlockProps) {
  const { register } = useFormContext();
  const id = block.id ?? "";
  return (
    <NumberField
      label={(block.props.label as string) ?? id}
      placeholder={(block.props.placeholder as string) || undefined}
      hint={(block.props.help as string) || undefined}
      error={useFieldError(id)}
      {...register(id, { valueAsNumber: true })}
    />
  );
}

function SelectInputBlock({ block }: BlockProps) {
  const { mode, values } = useBlockRender();
  const id = block.id ?? "";
  const label = (block.props.label as string) ?? id;
  if (mode === "submitted") {
    return (
      <SubmittedField label={label}>
        {formatScalar(values[id])}
      </SubmittedField>
    );
  }
  return <RHFSelect block={block} />;
}

function RHFSelect({ block }: BlockProps) {
  const { register } = useFormContext();
  const id = block.id ?? "";
  const options = (block.props.options as string[]) ?? [];
  const required = Boolean(block.props.required);
  return (
    <SelectField
      label={(block.props.label as string) ?? id}
      options={options}
      placeholder={required ? "Choose one…" : undefined}
      hint={(block.props.help as string) || undefined}
      error={useFieldError(id)}
      {...register(id)}
    />
  );
}

function TextareaInputBlock({ block }: BlockProps) {
  const { mode, values } = useBlockRender();
  const id = block.id ?? "";
  const label = (block.props.label as string) ?? id;
  if (mode === "submitted") {
    return (
      <SubmittedField label={label}>
        {formatScalar(values[id])}
      </SubmittedField>
    );
  }
  return <RHFTextarea block={block} />;
}

function RHFTextarea({ block }: BlockProps) {
  const { register } = useFormContext();
  const id = block.id ?? "";
  return (
    <TextareaField
      label={(block.props.label as string) ?? id}
      placeholder={(block.props.placeholder as string) || undefined}
      hint={(block.props.help as string) || undefined}
      error={useFieldError(id)}
      {...register(id)}
    />
  );
}

// --- Radio -----------------------------------------------------------------

function RadioInputBlock({ block }: BlockProps) {
  const { mode, values } = useBlockRender();
  const id = block.id ?? "";
  const label = (block.props.label as string) ?? id;
  if (mode === "submitted") {
    return (
      <SubmittedField label={label}>{formatScalar(values[id])}</SubmittedField>
    );
  }
  return <RHFRadio block={block} />;
}

function RHFRadio({ block }: BlockProps) {
  const { register } = useFormContext();
  const id = block.id ?? "";
  return (
    <RadioField
      label={(block.props.label as string) ?? id}
      options={(block.props.options as string[]) ?? []}
      hint={(block.props.help as string) || undefined}
      error={useFieldError(id)}
      {...register(id)}
    />
  );
}

// --- Single checkbox -------------------------------------------------------

function CheckboxInputBlock({ block }: BlockProps) {
  const { mode, values } = useBlockRender();
  const id = block.id ?? "";
  const label = (block.props.label as string) ?? id;
  if (mode === "submitted") {
    return (
      <SubmittedField label={label}>
        {values[id] ? "Yes" : "No"}
      </SubmittedField>
    );
  }
  return <RHFCheckbox block={block} />;
}

function RHFCheckbox({ block }: BlockProps) {
  const { register } = useFormContext();
  const id = block.id ?? "";
  return (
    <CheckboxField
      label={(block.props.label as string) ?? id}
      hint={(block.props.help as string) || undefined}
      error={useFieldError(id)}
      {...register(id)}
    />
  );
}

// --- Date ------------------------------------------------------------------

function DateInputBlock({ block }: BlockProps) {
  const { mode, values } = useBlockRender();
  const id = block.id ?? "";
  const label = (block.props.label as string) ?? id;
  if (mode === "submitted") {
    return (
      <SubmittedField label={label}>{formatScalar(values[id])}</SubmittedField>
    );
  }
  return <RHFDate block={block} />;
}

function RHFDate({ block }: BlockProps) {
  const { register } = useFormContext();
  const id = block.id ?? "";
  return (
    <TextField
      type="date"
      label={(block.props.label as string) ?? id}
      hint={(block.props.help as string) || undefined}
      min={(block.props.min as string) || undefined}
      max={(block.props.max as string) || undefined}
      error={useFieldError(id)}
      {...register(id)}
    />
  );
}

// --- Email / Phone / URL / Time --------------------------------------------
//
// Thin wrappers over TextField — the HTML input `type` drives native
// validation and the on-screen keyboard. Each takes the standard
// label/help/error treatment.

function makeTypedTextBlock(inputType: string) {
  function TypedTextBlock({ block }: BlockProps) {
    const { mode, values } = useBlockRender();
    const id = block.id ?? "";
    const label = (block.props.label as string) ?? id;
    if (mode === "submitted") {
      return (
        <SubmittedField label={label}>
          {formatScalar(values[id])}
        </SubmittedField>
      );
    }
    return <RHFTypedText block={block} inputType={inputType} />;
  }
  return TypedTextBlock;
}

function RHFTypedText({
  block,
  inputType,
}: BlockProps & { inputType: string }) {
  const { register } = useFormContext();
  const id = block.id ?? "";
  return (
    <TextField
      type={inputType}
      label={(block.props.label as string) ?? id}
      placeholder={(block.props.placeholder as string) || undefined}
      hint={(block.props.help as string) || undefined}
      min={(block.props.min as string) || undefined}
      max={(block.props.max as string) || undefined}
      error={useFieldError(id)}
      {...register(id)}
    />
  );
}

const EmailInputBlock = makeTypedTextBlock("email");
const PhoneInputBlock = makeTypedTextBlock("tel");
const URLInputBlock = makeTypedTextBlock("url");
const TimeInputBlock = makeTypedTextBlock("time");

// --- Rating ----------------------------------------------------------------

function RatingInputBlock({ block }: BlockProps) {
  const { mode, values } = useBlockRender();
  const id = block.id ?? "";
  const label = (block.props.label as string) ?? id;
  if (mode === "submitted") {
    const v = values[id];
    const max = (block.props.max as number) ?? 5;
    return (
      <SubmittedField label={label}>
        {v ? `${v} / ${max}` : "—"}
      </SubmittedField>
    );
  }
  return <RHFRating block={block} />;
}

function RHFRating({ block }: BlockProps) {
  const { setValue, watch } = useFormContext();
  const id = block.id ?? "";
  const max = (block.props.max as number) ?? 5;
  const current = (watch(id) as number) ?? 0;
  const error = useFieldError(id);
  return (
    <Field
      label={(block.props.label as string) ?? id}
      htmlFor={id}
      error={error}
      hint={(block.props.help as string) || undefined}
    >
      <div className="flex gap-1.5" role="radiogroup" aria-label={id}>
        {Array.from({ length: max }, (_, i) => i + 1).map((n) => (
          <button
            key={n}
            type="button"
            aria-label={`${n}`}
            aria-pressed={n <= current}
            onClick={() =>
              setValue(id, n, {
                shouldValidate: true,
                shouldDirty: true,
              })
            }
            className={[
              "h-9 w-9 rounded-theme border text-lg leading-none",
              "transition-colors",
              n <= current
                ? "border-accent bg-accent text-bg"
                : "border-border bg-surface text-muted hover:border-ink",
            ].join(" ")}
          >
            ★
          </button>
        ))}
      </div>
    </Field>
  );
}

// --- Slider ----------------------------------------------------------------

function SliderInputBlock({ block }: BlockProps) {
  const { mode, values } = useBlockRender();
  const id = block.id ?? "";
  const label = (block.props.label as string) ?? id;
  if (mode === "submitted") {
    return (
      <SubmittedField label={label}>
        {formatScalar(values[id])}
      </SubmittedField>
    );
  }
  return <RHFSlider block={block} />;
}

function RHFSlider({ block }: BlockProps) {
  const { register, watch } = useFormContext();
  const id = block.id ?? "";
  const min = (block.props.min as number) ?? 0;
  const max = (block.props.max as number) ?? 100;
  const step = (block.props.step as number) ?? 1;
  const current = watch(id);
  return (
    <Field
      label={(block.props.label as string) ?? id}
      htmlFor={id}
      error={useFieldError(id)}
      hint={(block.props.help as string) || undefined}
    >
      <div className="flex items-center gap-4">
        <input
          id={id}
          type="range"
          min={min}
          max={max}
          step={step}
          className="flex-1 accent-accent"
          {...register(id, { valueAsNumber: true })}
        />
        <span className="w-12 text-right font-mono text-sm text-ink">
          {current ?? min}
        </span>
      </div>
    </Field>
  );
}

// --- File uploads ----------------------------------------------------------
//
// Both `file` and `s3file` render with the same FileUploadField — the
// upload endpoint resolves where the bytes go from the form's spec.
// The field value is the upload reference object the endpoint returns.

function FileInputBlock({ block }: BlockProps) {
  const { mode, values } = useBlockRender();
  const id = block.id ?? "";
  const label = (block.props.label as string) ?? id;
  if (mode === "submitted") {
    const v = values[id] as { filename?: string } | undefined;
    return (
      <SubmittedField label={label}>
        {v?.filename ?? "—"}
      </SubmittedField>
    );
  }
  return <RHFFile block={block} />;
}

function RHFFile({ block }: BlockProps) {
  const { control, getValues } = useFormContext();
  const { formId, submissionId } = useBlockRender();
  const id = block.id ?? "";
  const accept = (block.props.accept as string[]) ?? [];
  const maxSizeMb = (block.props.max_size_mb as number) ?? 25;
  return (
    <Controller
      control={control}
      name={id}
      render={({ field, fieldState }) => (
        <FileUploadField
          label={(block.props.label as string) ?? id}
          formId={formId}
          fieldId={id}
          submissionId={submissionId}
          getDraftValues={getValues}
          accept={accept}
          maxSizeMb={maxSizeMb}
          hint={(block.props.help as string) || undefined}
          error={fieldState.error?.message}
          value={field.value ?? null}
          onChange={field.onChange}
        />
      )}
    />
  );
}

// --- Sankey mapping --------------------------------------------------------

function SankeyInputBlock({ block }: BlockProps) {
  const { mode, values } = useBlockRender();
  const id = block.id ?? "";
  const label = (block.props.label as string) ?? id;
  if (mode === "submitted") {
    const v = (values[id] as SankeyLink[]) ?? [];
    if (v.length === 0) {
      return <SubmittedField label={label}>—</SubmittedField>;
    }
    return (
      <SankeyField
        label={label}
        fieldId={id}
        columnA={(block.props.column_a as string[]) ?? []}
        columnB={(block.props.column_b as string[]) ?? []}
        normalize={(block.props.normalize as boolean) ?? true}
        value={v}
        onChange={() => {}}
        readOnly
      />
    );
  }
  return <RHFSankey block={block} />;
}

function RHFSankey({ block }: BlockProps) {
  const { control } = useFormContext();
  const id = block.id ?? "";
  return (
    <Controller
      control={control}
      name={id}
      render={({ field, fieldState }) => (
        <SankeyField
          label={(block.props.label as string) ?? id}
          fieldId={id}
          columnA={(block.props.column_a as string[]) ?? []}
          columnB={(block.props.column_b as string[]) ?? []}
          normalize={(block.props.normalize as boolean) ?? true}
          hint={(block.props.help as string) || undefined}
          error={fieldState.error?.message}
          value={field.value ?? []}
          onChange={field.onChange}
        />
      )}
    />
  );
}

// --- Date range ------------------------------------------------------------

function DateRangeInputBlock({ block }: BlockProps) {
  const { mode, values } = useBlockRender();
  const id = block.id ?? "";
  const label = (block.props.label as string) ?? id;
  if (mode === "submitted") {
    const v = values[id] as { start?: string; end?: string } | undefined;
    return (
      <SubmittedField label={label}>
        {v && (v.start || v.end)
          ? `${v.start || "—"} → ${v.end || "—"}`
          : "—"}
      </SubmittedField>
    );
  }
  return <RHFDateRange block={block} />;
}

function RHFDateRange({ block }: BlockProps) {
  const { control } = useFormContext();
  const id = block.id ?? "";
  return (
    <Controller
      control={control}
      name={id}
      render={({ field, fieldState }) => (
        <DateRangeField
          label={(block.props.label as string) ?? id}
          value={field.value}
          onChange={field.onChange}
          hint={(block.props.help as string) || undefined}
          error={fieldState.error?.message}
        />
      )}
    />
  );
}

// --- Number range ----------------------------------------------------------

function NumberRangeInputBlock({ block }: BlockProps) {
  const { mode, values } = useBlockRender();
  const id = block.id ?? "";
  const label = (block.props.label as string) ?? id;
  if (mode === "submitted") {
    const v = values[id] as { min?: number; max?: number } | undefined;
    const has = v && (v.min !== undefined || v.max !== undefined);
    return (
      <SubmittedField label={label}>
        {has ? `${v?.min ?? "—"} – ${v?.max ?? "—"}` : "—"}
      </SubmittedField>
    );
  }
  return <RHFNumberRange block={block} />;
}

function RHFNumberRange({ block }: BlockProps) {
  const { control } = useFormContext();
  const id = block.id ?? "";
  return (
    <Controller
      control={control}
      name={id}
      render={({ field, fieldState }) => (
        <NumberRangeField
          label={(block.props.label as string) ?? id}
          value={field.value}
          onChange={field.onChange}
          hint={(block.props.help as string) || undefined}
          error={fieldState.error?.message}
        />
      )}
    />
  );
}

// --- Multi-select ----------------------------------------------------------

function MultiSelectInputBlock({ block }: BlockProps) {
  const { mode, values } = useBlockRender();
  const id = block.id ?? "";
  const label = (block.props.label as string) ?? id;
  if (mode === "submitted") {
    const v = values[id] as string[] | undefined;
    return (
      <SubmittedField label={label}>
        {v && v.length > 0 ? v.join(", ") : "—"}
      </SubmittedField>
    );
  }
  return <RHFMultiSelect block={block} />;
}

function RHFMultiSelect({ block }: BlockProps) {
  const { control } = useFormContext();
  const id = block.id ?? "";
  return (
    <Controller
      control={control}
      name={id}
      render={({ field, fieldState }) => (
        <MultiSelectField
          label={(block.props.label as string) ?? id}
          options={(block.props.options as string[]) ?? []}
          value={field.value}
          onChange={field.onChange}
          hint={(block.props.help as string) || undefined}
          error={fieldState.error?.message}
        />
      )}
    />
  );
}

// --- Checkbox grid ---------------------------------------------------------

function CheckboxGridInputBlock({ block }: BlockProps) {
  const { mode, values } = useBlockRender();
  const id = block.id ?? "";
  const label = (block.props.label as string) ?? id;
  if (mode === "submitted") {
    const v = values[id] as Record<string, string[]> | undefined;
    const lines = v
      ? Object.entries(v)
          .filter(([, cols]) => cols.length > 0)
          .map(([row, cols]) => `${row}: ${cols.join(", ")}`)
      : [];
    return (
      <SubmittedField label={label}>
        {lines.length > 0 ? lines.join(" · ") : "—"}
      </SubmittedField>
    );
  }
  return <RHFCheckboxGrid block={block} />;
}

function RHFCheckboxGrid({ block }: BlockProps) {
  const { control } = useFormContext();
  const id = block.id ?? "";
  return (
    <Controller
      control={control}
      name={id}
      render={({ field, fieldState }) => (
        <CheckboxGridField
          label={(block.props.label as string) ?? id}
          rows={(block.props.rows as string[]) ?? []}
          columns={(block.props.columns as string[]) ?? []}
          value={field.value}
          onChange={field.onChange}
          hint={(block.props.help as string) || undefined}
          error={fieldState.error?.message}
        />
      )}
    />
  );
}

// --- Checkbox list ---------------------------------------------------------

function CheckboxListInputBlock({ block }: BlockProps) {
  const { mode, values } = useBlockRender();
  const id = block.id ?? "";
  const label = (block.props.label as string) ?? id;
  if (mode === "submitted") {
    const v = values[id] as string[] | undefined;
    return (
      <SubmittedField label={label}>
        {v && v.length > 0 ? v.join(", ") : "—"}
      </SubmittedField>
    );
  }
  return <RHFCheckboxList block={block} />;
}

function RHFCheckboxList({ block }: BlockProps) {
  const { control } = useFormContext();
  const id = block.id ?? "";
  return (
    <Controller
      control={control}
      name={id}
      render={({ field, fieldState }) => (
        <CheckboxListField
          label={(block.props.label as string) ?? id}
          options={(block.props.options as string[]) ?? []}
          columns={block.props.columns as number | undefined}
          value={field.value}
          onChange={field.onChange}
          hint={(block.props.help as string) || undefined}
          error={fieldState.error?.message}
        />
      )}
    />
  );
}

// --- Picker (dynamic-options field) ----------------------------------------

function PickerInputBlock({ block }: BlockProps) {
  const { mode, values } = useBlockRender();
  const id = block.id ?? "";
  const label = (block.props.label as string) ?? id;
  if (mode === "submitted") {
    const v = values[id];
    if (Array.isArray(v)) {
      return (
        <SubmittedField label={label}>
          {v.length > 0 ? v.join(", ") : "—"}
        </SubmittedField>
      );
    }
    return (
      <SubmittedField label={label}>{formatScalar(v)}</SubmittedField>
    );
  }
  const multi = Boolean(block.props.multi);
  return <RHFPicker block={block} multi={multi} />;
}

function RHFPicker({ block, multi }: BlockProps & { multi: boolean }) {
  const { control } = useFormContext();
  const { formId, nodeId } = useBlockRender();
  const id = block.id ?? "";
  const label = (block.props.label as string) ?? id;
  const required = Boolean(block.props.required);
  const help = (block.props.help as string) || undefined;
  const error = useFieldError(id);
  const { options, loading, error: loadError } = usePickerOptions(
    formId,
    nodeId,
    id,
  );
  const hint =
    loadError != null ? `Failed to load options: ${loadError}` : help;

  return (
    <Controller
      control={control}
      name={id}
      render={({ field }) => {
        if (multi) {
          // Multi mode: checkbox column. Each toggle compares by
          // stringified identity so a number-typed option lined up
          // against a string-typed selection still finds itself.
          const arr: Array<string | number> = Array.isArray(field.value)
            ? field.value
            : [];
          const toggle = (val: string | number) => {
            if (arr.some((x) => String(x) === String(val))) {
              field.onChange(
                arr.filter((x) => String(x) !== String(val)),
              );
            } else {
              field.onChange([...arr, val]);
            }
          };
          return (
            <Field
              label={label}
              htmlFor={id}
              error={error}
              hint={hint}
            >
              <div className="flex flex-col gap-2">
                {loading ? (
                  <div className="text-sm text-muted italic">Loading…</div>
                ) : options.length === 0 ? (
                  <div className="text-sm text-muted italic">
                    No options available.
                  </div>
                ) : (
                  options.map((opt) => {
                    const checked = arr.some(
                      (x) => String(x) === String(opt.value),
                    );
                    return (
                      <label
                        key={String(opt.value)}
                        className="flex items-center gap-2 cursor-pointer"
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => toggle(opt.value)}
                          className="w-4 h-4"
                        />
                        <span className="text-base text-ink">
                          {opt.label}
                        </span>
                      </label>
                    );
                  })
                )}
              </div>
            </Field>
          );
        }
        // Single mode: native select. value is coerced to string for
        // the DOM; on change we restore the original typed value
        // (string or number) by looking it up in the options list.
        const stringValue =
          field.value == null ? "" : String(field.value);
        return (
          <Field label={label} htmlFor={id} error={error} hint={hint}>
            <div className="relative">
              <select
                id={id}
                value={stringValue}
                onChange={(e) => {
                  const v = e.target.value;
                  const opt = options.find(
                    (o) => String(o.value) === v,
                  );
                  field.onChange(opt ? opt.value : v || null);
                }}
                disabled={loading}
                className={[
                  "w-full bg-surface border border-border rounded-theme",
                  "appearance-none px-4 py-3 pr-10 text-base text-ink",
                  "font-sans outline-none transition-colors cursor-pointer",
                  "focus:border-ink",
                  error ? "border-error" : "",
                  loading ? "opacity-50" : "",
                ].join(" ")}
              >
                <option value="" disabled>
                  {loading
                    ? "Loading…"
                    : required
                      ? "Choose one…"
                      : "—"}
                </option>
                {options.map((opt) => (
                  <option
                    key={String(opt.value)}
                    value={String(opt.value)}
                  >
                    {opt.label}
                  </option>
                ))}
              </select>
              <span
                aria-hidden
                className="pointer-events-none absolute right-4 top-1/2 -translate-y-1/2 text-muted"
              >
                ▾
              </span>
            </div>
          </Field>
        );
      }}
    />
  );
}

// --- Histogram widget block ------------------------------------------------

function HistogramWidgetBlock({ block }: BlockProps) {
  const { mode, values } = useBlockRender();
  const id = block.id ?? "";
  const widget = getWidget("distribution_filter");
  const field = synthWidgetField(block);

  if (!widget) {
    return (
      <div className="border border-error bg-surface p-3 text-sm text-error">
        Unknown widget
      </div>
    );
  }

  if (mode === "submitted") {
    const v = values[id];
    return (
      <SubmittedField label={field.label}>
        {v !== null && v !== undefined ? widget.renderSubmitted(v, field) : "—"}
      </SubmittedField>
    );
  }
  return <RHFWidget block={block} />;
}

function CategorizerWidgetBlock({ block }: BlockProps) {
  // Drag-to-classify board. The item bank arrives in `props.options`
  // (resolved server-side from `options_from`); columns + bank label
  // ride the synthesized field's widget_data.
  const { mode, values } = useBlockRender();
  const { control } = useFormContext();
  const id = block.id ?? "";
  const widget = getWidget("categorizer");
  const field = synthCategorizerField(block);

  if (!widget) {
    return (
      <div className="border border-error bg-surface p-3 text-sm text-error">
        Unknown widget
      </div>
    );
  }

  if (mode === "submitted") {
    const v = values[id];
    return (
      <SubmittedField label={field.label}>
        {v !== null && v !== undefined ? widget.renderSubmitted(v, field) : "—"}
      </SubmittedField>
    );
  }

  const xcom = { [id]: block.props.options };
  const WidgetComponent = widget.Component;
  return (
    <LockableWidget>
    <Controller
      control={control}
      name={id}
      render={({ field: rhf, fieldState }) => (
        <WidgetComponent
          field={field}
          xcom={xcom}
          value={rhf.value}
          onChange={rhf.onChange}
          error={fieldState.error?.message}
        />
      )}
    />
    </LockableWidget>
  );
}

/**
 * A standalone comment thread block (`displays.Comments`) — an
 * always-visible inline discussion. Component-ANCHORED threads
 * (`.with_comments()`) render via the dispatch wrapper instead.
 * Deliberately not a form field: stays live on submitted nodes.
 */
function CommentsBlock({ block }: BlockProps) {
  const { formId, submissionId } = useBlockRender();
  const id = block.id ?? "";
  // No submission yet (landing draft) — a thread has nothing to
  // anchor to until the submission exists.
  if (!formId || !submissionId || !id) return null;
  return (
    <div className="flex flex-col gap-2 border border-border bg-surface p-4">
      <CommentThread
        formId={formId}
        submissionId={submissionId}
        threadId={id}
        label={(block.props.label as string) ?? "Comments"}
        placeholder={(block.props.placeholder as string) ?? undefined}
      />
    </div>
  );
}

/**
 * Wraps a custom-interaction widget so locked mode (a submitted node
 * keeping its full layout) freezes its pointer-driven behavior —
 * drags, brushes, chip moves. Native inputs are already covered by
 * the surrounding fieldset[disabled]; this handles the div/SVG
 * handlers a fieldset can't reach.
 */
function LockableWidget({ children }: { children: ReactNode }) {
  const { locked } = useBlockRender();
  return (
    <div className={locked ? "pointer-events-none" : undefined}>
      {children}
    </div>
  );
}

function RHFWidget({ block }: BlockProps) {
  const { control } = useFormContext();
  const id = block.id ?? "";
  const widget = getWidget("distribution_filter")!;
  const field = synthWidgetField(block);
  const xcom = { [id]: block.props.data };
  const WidgetComponent = widget.Component;
  return (
    <LockableWidget>
    <Controller
      control={control}
      name={id}
      render={({ field: rhf, fieldState }) => (
        <WidgetComponent
          field={field}
          xcom={xcom}
          value={rhf.value}
          onChange={rhf.onChange}
          error={fieldState.error?.message}
        />
      )}
    />
    </LockableWidget>
  );
}

// --- Redistribution editor block ------------------------------------------

function RedistributionWidgetBlock({ block }: BlockProps) {
  const { mode, values } = useBlockRender();
  const id = block.id ?? "";
  const widget = getWidget("redistribution_editor");
  const field = synthRedistributionField(block);

  if (!widget) {
    return (
      <div className="border border-error bg-surface p-3 text-sm text-error">
        Unknown widget
      </div>
    );
  }

  if (mode === "submitted") {
    const v = values[id];
    return (
      <SubmittedField label={field.label}>
        {v !== null && v !== undefined ? widget.renderSubmitted(v, field) : "—"}
      </SubmittedField>
    );
  }
  return <RHFRedistribution block={block} />;
}

function RHFRedistribution({ block }: BlockProps) {
  const { control } = useFormContext();
  const id = block.id ?? "";
  const widget = getWidget("redistribution_editor")!;
  const field = synthRedistributionField(block);
  // Pack the resolved data + sources + destinations into the xcom
  // bundle the widget expects. The block-level RPC layer already
  // resolved any StepRefs (runtime.py `_resolve_block`) so these
  // are plain JSON now.
  const xcom = {
    [id]: {
      data: block.props.data,
      sources: block.props.sources,
      destinations: block.props.destinations,
    },
  };
  const WidgetComponent = widget.Component;
  return (
    <LockableWidget>
    <Controller
      control={control}
      name={id}
      render={({ field: rhf, fieldState }) => (
        <WidgetComponent
          field={field}
          xcom={xcom}
          value={rhf.value}
          onChange={rhf.onChange}
          error={fieldState.error?.message}
        />
      )}
    />
    </LockableWidget>
  );
}

// --- Button block ----------------------------------------------------------

// --- Buttons ---------------------------------------------------------------

/** Shared button geometry/typography for both submit and link buttons. */
const BUTTON_BASE =
  "inline-flex items-center justify-center gap-2 px-6 py-3 font-sans " +
  "text-sm uppercase tracking-[0.18em] text-center leading-tight " +
  "rounded-theme transition duration-200";

/** Themed per-variant styling — theme tokens only, no hardcoded colors. */
const BUTTON_VARIANTS: Record<string, string> = {
  primary: "bg-ink text-bg hover:bg-accent-hover",
  secondary: "bg-surface text-ink border border-border hover:border-ink",
  danger: "bg-error text-bg hover:opacity-90",
};

function variantClass(variant: unknown): string {
  return BUTTON_VARIANTS[variant as string] ?? BUTTON_VARIANTS.primary;
}

function ButtonBlock({ block }: BlockProps) {
  const { mode, clickedButton, locked } = useBlockRender();
  const id = block.id ?? "";
  const label = (block.props.label as string) ?? "Submit";
  const variant = block.props.variant;
  const url = block.props.url as string | undefined;

  // Link button — navigates, never submits; identical in both modes.
  if (url) {
    return (
      <LinkButton
        label={label}
        url={url}
        variant={variant}
        newTab={block.props.new_tab !== false}
      />
    );
  }

  // Locked form mode (a submitted node keeping its full layout)
  // renders buttons exactly like submitted mode: only the clicked
  // one, as a chosen-chip.
  if (mode === "submitted" || locked) {
    if (clickedButton && clickedButton !== id) return null;
    return (
      <div className="inline-flex items-center gap-2 font-sans text-sm">
        <span className="text-accent font-bold">✓</span>
        <span className="uppercase tracking-[0.18em] text-ink">{label}</span>
      </div>
    );
  }
  return <FormButton id={id} label={label} variant={variant} />;
}

function FormButton({
  id,
  label,
  variant,
}: {
  id: string;
  label: string;
  variant: unknown;
}) {
  const nodeForm = useNodeForm();
  const inRow = useContext(RowContext);
  if (!nodeForm) return null;
  // The active button reflects the current phase so the user knows
  // whether files are still uploading or the step is posting.
  const isActive = nodeForm.pendingButton === id || nodeForm.uploadPhase !== "idle";
  const busy =
    nodeForm.isSubmitting || nodeForm.uploadPhase !== "idle";
  const phaseLabel =
    nodeForm.uploadPhase === "uploading"
      ? "Uploading…"
      : nodeForm.uploadPhase === "submitting" || nodeForm.isSubmitting
        ? "Submitting…"
        : null;
  return (
    <button
      type="button"
      disabled={busy}
      onClick={() => nodeForm.submitWith(id)}
      className={[
        BUTTON_BASE,
        variantClass(variant),
        "disabled:opacity-50 disabled:cursor-not-allowed",
        inRow ? "w-full" : "",
      ].join(" ")}
    >
      {isActive && phaseLabel ? phaseLabel : label}
    </button>
  );
}

function LinkButton({
  label,
  url,
  variant,
  newTab,
}: {
  label: string;
  url: string;
  variant: unknown;
  newTab: boolean;
}) {
  const inRow = useContext(RowContext);
  return (
    <a
      href={url}
      target={newTab ? "_blank" : undefined}
      rel={newTab ? "noreferrer" : undefined}
      className={[
        BUTTON_BASE,
        variantClass(variant),
        "no-underline",
        inRow ? "w-full" : "",
      ].join(" ")}
    >
      {label}
    </a>
  );
}

// --- Registry + dispatch ---------------------------------------------------

function UnknownBlock({ block }: BlockProps) {
  return (
    <div className="border border-error bg-surface p-3 text-sm text-error font-mono">
      Unknown block type: {block.type}
    </div>
  );
}

/** Bridges the layout tree's untyped props to the dashboard embed, and
 *  supplies the form id the embed endpoints are scoped to. */
function DashboardBlock({ block }: BlockProps) {
  const { formId, submissionId } = useBlockRender();
  return (
    <DashboardEmbed
      formId={formId ?? null}
      submissionId={submissionId ?? null}
      name={(block.props.name as string) ?? ""}
      height={(block.props.height as number) ?? 600}
      showFilters={Boolean(block.props.show_filters)}
      filtersExpanded={Boolean(block.props.filters_expanded)}
    />
  );
}

const REGISTRY: Record<string, ComponentType<BlockProps>> = {
  column: ColumnBlock,
  row: RowBlock,
  card: CardBlock,
  section: SectionBlock,
  callout: CalloutBlock,
  collapsible: CollapsibleBlock,
  when: WhenBlock,
  markdown: MarkdownBlock,
  divider: DividerBlock,
  image: ImageBlock,
  kpi: KPIBlock,
  kpi_groups: KPIGroupsBlock,
  comments: CommentsBlock,
  figure: FigureBlock,
  s3_download: S3DownloadBlock,
  dashboard: DashboardBlock,
  table: TableBlock,
  text: TextInputBlock,
  number: NumberInputBlock,
  select: SelectInputBlock,
  textarea: TextareaInputBlock,
  radio: RadioInputBlock,
  checkbox: CheckboxInputBlock,
  date: DateInputBlock,
  email: EmailInputBlock,
  tel: PhoneInputBlock,
  url: URLInputBlock,
  time: TimeInputBlock,
  rating: RatingInputBlock,
  slider: SliderInputBlock,
  file: FileInputBlock,
  s3file: FileInputBlock,
  sankey: SankeyInputBlock,
  date_range: DateRangeInputBlock,
  number_range: NumberRangeInputBlock,
  multi_select: MultiSelectInputBlock,
  checkbox_grid: CheckboxGridInputBlock,
  checkbox_list: CheckboxListInputBlock,
  picker: PickerInputBlock,
  histogram_widget: HistogramWidgetBlock,
  redistribution_widget: RedistributionWidgetBlock,
  widget_categorizer: CategorizerWidgetBlock,
  button: ButtonBlock,
};

/**
 * Resolve `{{ steps.<node>.<field> }}` templates in a block's `label`
 * and `url` props against the form's live values. Same-node tokens
 * resolve here as the user types; other-node tokens were already
 * resolved server-side. Only allocates a new block when a prop
 * actually contains a template.
 */
function useResolvedBlock(block: Block): Block {
  const values = useWatch() as Record<string, unknown> | undefined;
  const { nodeId } = useBlockRender();
  const label = block.props.label;
  const url = block.props.url;
  const hasLabel = typeof label === "string" && label.includes("{{");
  const hasUrl = typeof url === "string" && url.includes("{{");
  if (!hasLabel && !hasUrl) return block;
  const v = values ?? {};
  const props = { ...block.props };
  if (hasLabel) props.label = interpolate(label as string, v, nodeId);
  if (hasUrl) {
    props.url = interpolate(url as string, v, nodeId, { encode: true });
  }
  return { ...block, props };
}

export function BlockTree({ block }: BlockProps) {
  const resolved = useResolvedBlock(block);
  const { locked, formId, submissionId } = useBlockRender();
  const Component = REGISTRY[resolved.type] ?? UnknownBlock;
  // Locked layout (a submitted node keeping its full composition):
  // each INPUT block gets its own disabling fieldset, so non-input
  // interactive blocks — Collapsible toggles, Comments composers —
  // stay live. Custom pointer widgets add LockableWidget on top.
  let content =
    locked && FIELD_TYPES.has(resolved.type) ? (
      <fieldset disabled className="block min-w-0 border-0 p-0 m-0">
        <Component block={resolved} />
      </fieldset>
    ) : (
      <Component block={resolved} />
    );
  // `.with_comments()` attachment: a corner bubble opens the
  // component's thread, Google-Docs style. Needs a submission to
  // anchor to — hidden on the landing draft.
  const thread = resolved.props.comment_thread as
    | { id: string; label?: string | null }
    | undefined;
  if (thread?.id && formId && submissionId) {
    content = (
      <div className="relative">
        {content}
        <div className="absolute top-1 right-1 z-10">
          <CommentToggle
            formId={formId}
            submissionId={submissionId}
            threadId={thread.id}
            label={thread.label ?? "Comments"}
          />
        </div>
      </div>
    );
  }
  return content;
}

// --- helpers ---------------------------------------------------------------

function formatScalar(value: unknown): ReactNode {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "number" && Number.isNaN(value)) return "—";
  return String(value);
}
