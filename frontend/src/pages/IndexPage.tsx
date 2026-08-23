import { useCallback, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { ApiError, listWorkspaces, type FormSummary } from "../lib/api";
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
 * The index — every form and workspace, shelved by the folder its DSL
 * file lives in.
 *
 * A folder holds whatever was declared in it, rather than one kind of
 * thing: a workspace usually sits beside the forms it shows, and
 * separating them by type would put the door in a different room from
 * the thing it opens.
 *
 * Folders collapse, and which ones are open is remembered per browser.
 * Nothing here is per-user server state — it is a view preference, and
 * one that would be irritating to lose on every reload.
 */

const OPEN_FOLDERS_KEY = "frontflow.index.openFolders";

function readOpenFolders(): Set<string> {
  try {
    const raw = window.localStorage.getItem(OPEN_FOLDERS_KEY);
    if (!raw) return new Set();
    const parsed: unknown = JSON.parse(raw);
    return new Set(Array.isArray(parsed) ? (parsed as string[]) : []);
  } catch {
    return new Set();
  }
}

function writeOpenFolders(open: Set<string>): void {
  try {
    window.localStorage.setItem(OPEN_FOLDERS_KEY, JSON.stringify([...open]));
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

  const [open, setOpen] = useState<Set<string>>(readOpenFolders);
  const toggleFolder = useCallback((path: string) => {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      writeOpenFolders(next);
      return next;
    });
  }, []);

  const tree = useMemo(() => {
    const items: IndexItem[] = [
      ...(workspaces.data ?? []).map((w) => ({
        kind: "workspace" as const,
        id: w.workspace_id,
        title: w.title,
        folder: w.folder_path ?? "",
        description: w.description,
        tags: w.tags,
      })),
      ...(forms ?? []).map((f) => ({
        kind: "form" as const,
        id: f.form_id,
        title: f.name,
        folder: f.folder_path ?? "",
        meta: { form: f },
      })),
    ];
    return buildFolderTree(items);
  }, [forms, workspaces.data]);

  const total = countItems(tree);

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
        <Folder node={tree} open={open} onToggle={toggleFolder} depth={0} />
      )}
    </main>
  );
}

/**
 * One folder and everything under it. The root renders its own contents
 * without a header — there is no folder to name, and a disclosure
 * triangle on "everything" would only ever be open.
 */
function Folder({
  node,
  open,
  onToggle,
  depth,
}: {
  node: FolderNode;
  open: Set<string>;
  onToggle: (path: string) => void;
  depth: number;
}) {
  const isRoot = node.path === "";
  const expanded = isRoot || open.has(node.path);

  return (
    <section>
      {!isRoot && (
        <button
          type="button"
          onClick={() => onToggle(node.path)}
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
              open={open}
              onToggle={onToggle}
              depth={isRoot ? depth : depth + 1}
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
      : `/forms/${encodeURIComponent(item.id)}`;
  const form = item.meta?.form as FormSummary | undefined;

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
        {item.kind === "workspace" ? "Space" : "Form"}
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
        </div>
        {item.description && (
          <p className="mt-1 truncate text-xs text-muted">{item.description}</p>
        )}
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
