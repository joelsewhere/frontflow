import { useCallback, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import {
  ApiError,
  listStories,
  listWorkspaces,
  type FormSummary,
  type StorySummary,
} from "../lib/api";
import {
  buildFolderTree,
  countItems,
  type FolderNode,
  type IndexItem,
} from "../lib/folderTree";
import { formatTimestamp } from "../lib/format";
import { useFormsList } from "../hooks/useFormsList";
import { SignOutControl } from "../components/console/SignOutControl";
import { useAuth } from "../auth/AuthContext";

/**
 * The index — forms and workspaces, each under its own heading, and
 * within a heading arranged the way the Python files are.
 *
 * Two levels of grouping, and they answer different questions. The
 * heading answers "what kind of thing am I looking for"; the folders
 * under it answer "where does it live", which is the source tree
 * itself — `ops/intake/returns.py` becomes `ops → intake → returns`.
 * Nothing declares a folder; moving the file moves the entry.
 *
 * Everything collapses, and what is open is remembered per browser.
 * That is a view preference rather than server state, and one that
 * would be irritating to lose on every reload.
 */

const TOGGLED_KEY = "frontflow.index.toggled";

function readToggled(): Set<string> {
  try {
    const raw = window.localStorage.getItem(TOGGLED_KEY);
    if (!raw) return new Set();
    const parsed: unknown = JSON.parse(raw);
    return new Set(Array.isArray(parsed) ? (parsed as string[]) : []);
  } catch {
    return new Set();
  }
}

/**
 * Which entries are NOT in their default state.
 *
 * Defaults differ by level and both are deliberate: a heading opens,
 * because an index that starts fully shut shows nothing; a folder stays
 * shut, because the source tree can be deep and the point is to scan
 * what is there. So this set holds headings that were closed AND
 * folders that were opened — "toggled", not "open".
 */
function writeToggled(toggled: Set<string>): void {
  try {
    window.localStorage.setItem(TOGGLED_KEY, JSON.stringify([...toggled]));
  } catch {
    // Private browsing or a full quota — the tree still works, it just
    // reopens at the default next time.
  }
}

export default function IndexPage() {
  const { user } = useAuth();
  const { data: forms, error, isLoading } = useFormsList();
  const workspaces = useQuery({
    queryKey: ["workspaces"],
    queryFn: listWorkspaces,
  });
  const stories = useQuery({ queryKey: ["stories"], queryFn: listStories });

  const [toggled, setToggled] = useState<Set<string>>(readToggled);
  const toggleFolder = useCallback((path: string) => {
    setToggled((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      writeToggled(next);
      return next;
    });
  }, []);

  // One tree per kind: the heading groups by type, the folders inside
  // it mirror the source tree.
  const formTree = useMemo(
    () =>
      buildFolderTree(
        (forms ?? []).map((f) => ({
          kind: "form" as const,
          id: f.form_id,
          title: f.name,
          folder: f.folder_path ?? "",
          meta: { form: f },
        })),
      ),
    [forms],
  );

  const workspaceTree = useMemo(
    () =>
      buildFolderTree(
        (workspaces.data ?? []).map((w) => ({
          kind: "workspace" as const,
          id: w.workspace_id,
          title: w.title,
          folder: w.folder_path ?? "",
          description: w.description,
          tags: w.tags,
        })),
      ),
    [workspaces.data],
  );

  const storyTree = useMemo(
    () =>
      buildFolderTree(
        (stories.data ?? []).map((st) => ({
          kind: "story" as const,
          // The path within the source tree is the story's identity
          // everywhere: the DSL names it, the CLI renders it, the URL
          // routes to it.
          id: st.name,
          title: st.title,
          folder: st.folder,
          description: st.description ?? undefined,
          tags: st.categories,
          meta: { story: st },
        })),
      ),
    [stories.data],
  );

  const total =
    countItems(formTree) + countItems(workspaceTree) + countItems(storyTree);

  return (
    <main className="relative z-10 mx-auto max-w-7xl px-6 pt-24 pb-16">
      <header className="mb-12 flex items-end justify-between gap-6">
        <div>
          <h1 className="font-display text-5xl font-bold leading-[1.0] text-ink">
            Index
          </h1>
          {!isLoading && (
            <p className="mt-4 font-mono text-xs uppercase tracking-wider text-muted">
              {total} {total === 1 ? "item" : "items"}
            </p>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-6">
          {user?.is_admin && (
            <>
              <Link
                to="/users"
                className="font-mono text-xs uppercase tracking-wider text-muted hover:text-accent"
              >
                Users →
              </Link>
              <Link
                to="/access"
                className="font-mono text-xs uppercase tracking-wider text-muted hover:text-accent"
              >
                Access →
              </Link>
              <Link
                to="/connections"
                className="font-mono text-xs uppercase tracking-wider text-muted hover:text-accent"
              >
                Connections →
              </Link>
            </>
          )}
          <SignOutControl />
        </div>
      </header>

      {isLoading ? (
        <p className="font-display text-2xl text-ink opacity-30">Loading…</p>
      ) : error ? (
        <div className="border border-error bg-surface p-6">
          <p className="text-error text-sm">
            Couldn't load forms:{" "}
            {error instanceof ApiError ? error.message : "unknown error"}
          </p>
        </div>
      ) : total === 0 ? (
        <p className="text-muted text-sm">
          Nothing is registered yet.
        </p>
      ) : (
        <div className="space-y-8">
          <Section
            title="Workspaces"
            node={workspaceTree}
            toggled={toggled}
            onToggle={toggleFolder}
          />
          <Section
            title="Forms"
            node={formTree}
            toggled={toggled}
            onToggle={toggleFolder}
          />
          {countItems(storyTree) > 0 && (
            <Section
              title="Stories"
              node={storyTree}
              toggled={toggled}
              onToggle={toggleFolder}
            />
          )}
        </div>
      )}
    </main>
  );
}


/**
 * One kind of thing — Forms, or Workspaces — with the source tree
 * under it.
 *
 * Collapse keys are namespaced by section. A folder called `ops` can
 * exist under both headings, and without the prefix opening one would
 * open the other.
 */
function Section({
  title,
  node,
  toggled,
  onToggle,
}: {
  title: string;
  node: FolderNode;
  toggled: Set<string>;
  onToggle: (path: string) => void;
}) {
  const key = `section:${title}`;
  // Open unless it was closed — the opposite default to a folder, and
  // why the stored set is "toggled" rather than "open".
  const expanded = !toggled.has(key);
  const count = countItems(node);

  return (
    <section>
      <button
        type="button"
        onClick={() => onToggle(key)}
        aria-expanded={expanded}
        className="flex w-full items-center gap-2 border-b border-border py-2 text-left hover:bg-surface"
      >
        <span
          aria-hidden
          className={`inline-block font-mono text-[10px] text-muted transition-transform ${
            expanded ? "rotate-90" : ""
          }`}
        >
          ▶
        </span>
        <span className="font-display text-lg font-bold text-ink">{title}</span>
        <span className="font-mono text-[10px] text-muted">{count}</span>
      </button>

      {expanded &&
        (count === 0 ? (
          <p className="py-3 pl-6 text-xs text-muted">
            None declared yet.
          </p>
        ) : (
          <Folder
            node={node}
            toggled={toggled}
            onToggle={onToggle}
            depth={0}
            keyPrefix={`${key}/`}
          />
        ))}
    </section>
  );
}

/**
 * One folder and everything under it. The root renders its own contents
 * without a header — there is no folder to name, and a disclosure
 * triangle on "everything" would only ever be open.
 */
function Folder({
  node,
  toggled,
  onToggle,
  depth,
  keyPrefix,
}: {
  node: FolderNode;
  toggled: Set<string>;
  onToggle: (path: string) => void;
  depth: number;
  /** Namespaces the collapse key, so a folder of the same name under
   *  another heading is a different folder. */
  keyPrefix: string;
}) {
  const isRoot = node.path === "";
  const collapseKey = `${keyPrefix}${node.path}`;
  // Closed unless it was opened.
  const expanded = isRoot || toggled.has(collapseKey);

  return (
    <section>
      {!isRoot && (
        <button
          type="button"
          onClick={() => onToggle(collapseKey)}
          aria-expanded={expanded}
          className="flex w-full items-center gap-2 border-b border-border py-2 text-left hover:bg-surface"
          style={{ paddingLeft: `${depth * 1.25}rem` }}
        >
          <span
            aria-hidden
            className={`inline-block font-mono text-[10px] text-muted transition-transform ${
              expanded ? "rotate-90" : ""
            }`}
          >
            ▶
          </span>
          <span className="font-mono text-xs uppercase tracking-[0.15em] text-ink">
            {node.name}
          </span>
          <span className="font-mono text-[10px] text-muted">
            {countItems(node)}
          </span>
        </button>
      )}

      {expanded && (
        <div>
          {node.items.map((item) => (
            <IndexRow
              key={`${item.kind}:${item.id}`}
              item={item}
              depth={isRoot ? depth : depth + 1}
            />
          ))}
          {node.folders.map((child) => (
            <Folder
              key={child.path}
              node={child}
              toggled={toggled}
              onToggle={onToggle}
              depth={isRoot ? depth : depth + 1}
              keyPrefix={keyPrefix}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function IndexRow({ item, depth }: { item: IndexItem; depth: number }) {
  const navigate = useNavigate();
  const to =
    item.kind === "workspace"
      ? `/workspaces/${encodeURIComponent(item.id)}`
      : item.kind === "story"
        // A story's id is a path within the source tree, so it stays a
        // path in the URL rather than one encoded segment.
        ? `/stories/${item.id}`
        : `/forms/${encodeURIComponent(item.id)}`;
  const form = item.meta?.form as FormSummary | undefined;
  const story = item.meta?.story as StorySummary | undefined;

  return (
    <div
      onClick={() => navigate(to)}
      className="flex cursor-pointer items-center gap-4 border-b border-border py-3 transition-colors hover:bg-surface"
      style={{ paddingLeft: `${depth * 1.25 + 1.25}rem` }}
    >
      <span
        aria-hidden
        className="w-16 shrink-0 font-mono text-[10px] uppercase tracking-wider text-muted"
      >
        {item.kind === "workspace"
          ? "Space"
          : item.kind === "story"
            ? "Story"
            : "Form"}
      </span>

      <div className="min-w-0 flex-1">
        <Link
          to={to}
          onClick={(e) => e.stopPropagation()}
          className="font-medium text-ink hover:text-accent"
        >
          {item.title}
        </Link>
        <div className="mt-0.5 flex items-center gap-2">
          <span className="font-mono text-xs text-muted">{item.id}</span>
          {form && !form.is_live && (
            <span className="border border-border px-1.5 font-mono text-[10px] uppercase tracking-wider text-muted">
              Archived
            </span>
          )}
          {story?.date && (
            <span className="font-mono text-xs text-muted">{story.date}</span>
          )}
          {story?.stale === true && (
            <span className="border border-warning px-1.5 font-mono text-[10px] uppercase tracking-wider text-warning">
              Stale
            </span>
          )}
          {story && !story.rendered && (
            <span className="border border-error px-1.5 font-mono text-[10px] uppercase tracking-wider text-error">
              Not rendered
            </span>
          )}
          {story?.cell_errors ? (
            <span className="border border-error px-1.5 font-mono text-[10px] uppercase tracking-wider text-error">
              {story.cell_errors} failed
            </span>
          ) : null}
        </div>
        {item.description && (
          <p className="mt-1 truncate text-xs text-muted">{item.description}</p>
        )}
        {story?.categories.length ? (
          <div className="mt-1 flex flex-wrap gap-1.5">
            {story.categories.map((c) => (
              <span
                key={c}
                className="border border-border px-1.5 font-mono text-[10px] uppercase tracking-wider text-muted"
              >
                {c}
              </span>
            ))}
          </div>
        ) : null}
      </div>

      {form && <Submissions form={form} />}
    </div>
  );
}

/** The counts the old table showed, kept on one line. */
function Submissions({ form }: { form: FormSummary }) {
  const { running, failed, total } = form.submissions;
  if (total === 0) {
    return <span className="shrink-0 font-mono text-xs text-muted">—</span>;
  }
  return (
    <div className="flex shrink-0 items-baseline gap-3 font-mono text-xs">
      <span className="tabular-nums text-ink">{total}</span>
      {running > 0 && <span className="text-accent">{running} running</span>}
      {failed > 0 && <span className="text-error">{failed} failed</span>}
      <span
        className="hidden text-muted sm:inline"
        title="Last activity"
      >
        {formatTimestamp(form.last_activity)}
      </span>
    </div>
  );
}
