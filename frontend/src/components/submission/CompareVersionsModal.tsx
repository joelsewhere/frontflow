import { useEffect, useMemo, useState } from "react";
import { Highlight, type PrismTheme } from "prism-react-renderer";
import {
  diffFormVersions,
  type DiffHunk,
  type DiffLine,
  type FormVersionDiffResponse,
  type VersionOption,
} from "../../lib/api";
import { formatVersion } from "../../lib/version";
import { Modal } from "../ui/Modal";

/**
 * Side-by-side compare modal for two `form_version` rows of the
 * same form. Renders a unified diff: each line gets a row colored
 * green / red / neutral by kind, with Prism-driven Python syntax
 * highlighting layered on top via per-token text colors. The two
 * concerns don't fight — the row background lives on the row,
 * Prism only colors the foreground tokens.
 *
 * UX rules:
 *   - From defaults to the version one step BEFORE To (the most
 *     common "what changed since last time I looked" check).
 *   - Picking the same version on both sides isn't an error;
 *     server returns bump='none' and the body reads "(identical)".
 *   - Direction matters — reversing From/To inverts the diff and
 *     classifies the same (a "minor" diff is "minor" either way).
 */

const diffTheme: PrismTheme = {
  // Match PythonSource's frontflowTheme — single source of truth
  // would be nicer, but it's a small palette and the contrast tweaks
  // needed against the colored row backgrounds are subtle. Keep
  // them aligned by hand.
  plain: {
    color: "var(--color-ink)",
    backgroundColor: "transparent",
  },
  styles: [
    {
      types: ["comment", "prolog", "doctype", "cdata"],
      style: { color: "var(--color-muted)", fontStyle: "italic" },
    },
    {
      types: ["string", "char", "attr-value", "regex", "variable"],
      style: { color: "#059669" },
    },
    {
      types: ["keyword", "selector", "important", "atrule"],
      style: { color: "#2563eb", fontWeight: "600" },
    },
    {
      types: ["function", "class-name"],
      style: { color: "#b45309" },
    },
    {
      types: ["number", "boolean", "constant"],
      style: { color: "#9333ea" },
    },
    {
      types: ["operator", "entity", "url"],
      style: { color: "var(--color-ink)" },
    },
    {
      types: ["punctuation"],
      style: { color: "var(--color-muted)" },
    },
    {
      types: ["builtin", "tag"],
      style: { color: "#0891b2" },
    },
    {
      types: ["decorator", "annotation"],
      style: { color: "#0891b2", fontWeight: "600" },
    },
  ],
};

/** A side-by-side row pairs one "from" line (left) with one "to"
 *  line (right). Both null wouldn't make sense; exactly one null
 *  means the change is unbalanced (a pure insertion or deletion).
 *  A context line appears on BOTH sides with identical content. */
interface SideBySideRow {
  left: DiffLine | null;
  right: DiffLine | null;
}

/** Pair the unified hunk's lines into left/right rows.
 *
 *  Within each "change block" (a contiguous run of non-context
 *  lines), `difflib.unified_diff` emits all removes followed by all
 *  adds. We pair them positionally: the i-th remove with the i-th
 *  add. When the counts differ (a block that mostly added but
 *  deleted one line, or vice versa), the leftover rows show with an
 *  empty placeholder on the side they don't exist on. This matches
 *  what GitHub / VS Code's side-by-side diff shows for the same
 *  unified input. */
function pairLines(lines: DiffLine[]): SideBySideRow[] {
  const rows: SideBySideRow[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.kind === "context") {
      rows.push({ left: line, right: line });
      i++;
      continue;
    }
    // Collect a run of removes, then a run of adds. Either run can
    // be empty (pure addition or pure deletion).
    const removes: DiffLine[] = [];
    while (i < lines.length && lines[i].kind === "remove") {
      removes.push(lines[i]);
      i++;
    }
    const adds: DiffLine[] = [];
    while (i < lines.length && lines[i].kind === "add") {
      adds.push(lines[i]);
      i++;
    }
    const n = Math.max(removes.length, adds.length);
    for (let j = 0; j < n; j++) {
      rows.push({
        left: removes[j] ?? null,
        right: adds[j] ?? null,
      });
    }
  }
  return rows;
}

/** A single code cell — line number gutter + syntax-highlighted
 *  text. Background color encodes diff kind (green/red/neutral); a
 *  null line renders an empty placeholder (the other side has an
 *  unbalanced add/remove). */
function DiffCell({
  line, side,
}: {
  line: DiffLine | null;
  side: "left" | "right";
}) {
  if (!line) {
    return <div className="min-w-0 flex-1 bg-surface-muted" />;
  }
  const bg =
    line.kind === "add"
      ? "bg-emerald-50"
      : line.kind === "remove"
        ? "bg-red-50"
        : "";
  const lineno = side === "left" ? line.from_lineno : line.to_lineno;
  return (
    <div className={`flex min-w-0 flex-1 ${bg}`}>
      <span className="w-10 shrink-0 select-none pr-2 text-right text-muted">
        {lineno ?? ""}
      </span>
      <Highlight
        code={line.text || " "}
        language="python"
        theme={diffTheme}
      >
        {({ tokens, getTokenProps }) => (
          <code className="min-w-0 flex-1 overflow-x-auto whitespace-pre">
            {tokens[0]?.map((token, j) => (
              <span key={j} {...getTokenProps({ token })} />
            ))}
          </code>
        )}
      </Highlight>
    </div>
  );
}

function SideBySideRow({ row }: { row: SideBySideRow }) {
  return (
    <div className="flex font-mono text-[12px] leading-relaxed">
      <DiffCell line={row.left} side="left" />
      <div className="w-px shrink-0 bg-border" />
      <DiffCell line={row.right} side="right" />
    </div>
  );
}

function HunkBlock({ hunk }: { hunk: DiffHunk }) {
  const rows = useMemo(() => pairLines(hunk.lines), [hunk.lines]);
  return (
    <div className="border-t border-border">
      <div className="bg-surface-muted px-3 py-1 font-mono text-[11px] text-muted">
        {hunk.header}
      </div>
      <div>
        {rows.map((row, i) => (
          <SideBySideRow key={i} row={row} />
        ))}
      </div>
    </div>
  );
}

interface VersionSelectProps {
  label: string;
  options: VersionOption[];
  selected: number;
  onChange: (id: number) => void;
}

function VersionSelect({
  label, options, selected, onChange,
}: VersionSelectProps) {
  // Newest first — matches the picker on the summary page.
  const ordered = useMemo(
    () => [...options].sort((a, b) => {
      if (a.version !== b.version) return b.version - a.version;
      return b.minor_version - a.minor_version;
    }),
    [options],
  );
  return (
    <label className="flex items-center gap-2">
      <span className="font-mono text-[11px] uppercase tracking-wider text-muted">
        {label}
      </span>
      <select
        value={selected}
        onChange={(e) => onChange(Number(e.target.value))}
        className="border border-muted bg-surface px-2 py-1 font-mono text-xs text-ink hover:border-ink focus:outline-none focus:ring-1 focus:ring-accent"
      >
        {ordered.map((opt) => (
          <option key={opt.id} value={opt.id}>
            {formatVersion(opt.version, opt.minor_version)}
            {opt.is_active ? " (active)" : ""}
          </option>
        ))}
      </select>
    </label>
  );
}

export function CompareVersionsModal({
  open,
  onClose,
  formId,
  versions,
  initialFromId,
  initialToId,
}: {
  open: boolean;
  onClose: () => void;
  formId: string;
  versions: VersionOption[];
  /** Default FROM. Caller picks (usually one-back from the active
   *  version). */
  initialFromId: number;
  /** Default TO. Caller picks (usually the active version). */
  initialToId: number;
}) {
  const [fromId, setFromId] = useState(initialFromId);
  const [toId, setToId] = useState(initialToId);
  const [diff, setDiff] = useState<FormVersionDiffResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // Reset selection whenever the modal re-opens so it picks up new
  // defaults rather than persisting stale state from the last time.
  useEffect(() => {
    if (open) {
      setFromId(initialFromId);
      setToId(initialToId);
      setDiff(null);
      setError(null);
    }
  }, [open, initialFromId, initialToId]);

  // Fetch the diff whenever the (from, to) pair changes. Cleanup
  // flag handles the case where the user changes selection mid-
  // flight — only the latest request's response is honored.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    diffFormVersions(formId, fromId, toId)
      .then((d) => {
        if (cancelled) return;
        setDiff(d);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, formId, fromId, toId]);

  return (
    <Modal open={open} onClose={onClose} widthClass="max-w-[110rem]">
      <div className="flex max-h-[80vh] w-full flex-col gap-4">
        <header className="flex flex-col gap-3">
          <h2 className="font-display text-base uppercase tracking-[0.18em] text-ink">
            Compare versions
          </h2>
          <div className="flex flex-wrap items-center gap-3">
            <VersionSelect
              label="From"
              options={versions}
              selected={fromId}
              onChange={setFromId}
            />
            <span className="text-muted">→</span>
            <VersionSelect
              label="To"
              options={versions}
              selected={toId}
              onChange={setToId}
            />
            <button
              type="button"
              onClick={() => {
                const a = fromId;
                setFromId(toId);
                setToId(a);
              }}
              className="border border-muted bg-surface px-2 py-1 font-mono text-[11px] uppercase tracking-wider text-muted hover:border-ink hover:text-ink"
              title="Swap From and To"
            >
              ⇄ Swap
            </button>
          </div>
          {diff && diff.bump !== "none" ? (
            <p className="font-mono text-xs text-muted">
              {diff.hunks.length} hunk{diff.hunks.length === 1 ? "" : "s"}
              <span className="mx-2 text-emerald-700">
                +{diff.added_lines}
              </span>
              <span className="text-red-700">
                −{diff.removed_lines}
              </span>
              <span className="ml-2">
                — {diff.bump} change
              </span>
            </p>
          ) : null}
        </header>

        <div className="flex flex-col overflow-hidden border border-border">
          {diff && diff.bump !== "none" && diff.hunks.length > 0 ? (
            // Two-column header — sticky to the top of the scroll
            // area so the side labels stay visible while scrolling
            // long diffs. Mirrors the SideBySideRow flex layout so
            // the labels align exactly with the code columns below
            // (including the central divider).
            <div className="flex shrink-0 border-b border-border bg-surface-muted">
              <div className="flex-1 px-3 py-1 font-mono text-[11px] uppercase tracking-wider text-muted">
                From — {formatVersion(
                  diff.from_version.version,
                  diff.from_version.minor_version,
                )}
              </div>
              <div className="w-px shrink-0 bg-border" />
              <div className="flex-1 px-3 py-1 font-mono text-[11px] uppercase tracking-wider text-muted">
                To — {formatVersion(
                  diff.to_version.version,
                  diff.to_version.minor_version,
                )}
              </div>
            </div>
          ) : null}
          <div className="overflow-auto">
            {loading ? (
              <p className="px-3 py-4 text-sm text-muted">Loading diff…</p>
            ) : error ? (
              <p className="px-3 py-4 text-sm text-error">{error}</p>
            ) : !diff ? null : diff.bump === "none" ? (
              <p className="px-3 py-4 text-sm text-muted">
                These versions are identical.
              </p>
            ) : diff.hunks.length === 0 ? (
              <p className="px-3 py-4 text-sm text-muted">
                No differences in this section.
              </p>
            ) : (
              diff.hunks.map((h, i) => <HunkBlock key={i} hunk={h} />)
            )}
          </div>
        </div>

        <div className="flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="border border-muted bg-surface px-3 py-1 font-mono text-xs uppercase tracking-wider text-muted hover:border-ink hover:text-ink"
          >
            Close
          </button>
        </div>
      </div>
    </Modal>
  );
}
