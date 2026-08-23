/**
 * The index as a folder tree.
 *
 * Forms and workspaces are shelved by the folder their DSL file lives
 * in, so a folder holds whatever was declared in it rather than one
 * kind of thing. A path of "sales/intake" nests: the tree is built from
 * the segments, not from the string.
 *
 * Pure — no React, no fetching — so the shape of the tree can be
 * checked directly.
 */

export type IndexItemKind = "form" | "workspace";

export interface IndexItem {
  kind: IndexItemKind;
  /** Route-facing id: form_id or workspace_id. */
  id: string;
  title: string;
  /** "" is the root. "a/b" nests two deep. */
  folder: string;
  description?: string;
  tags?: string[];
  /** Anything else the row wants to render; carried through untouched. */
  meta?: Record<string, unknown>;
}

export interface FolderNode {
  /** Last segment — what the row displays. "" for the root. */
  name: string;
  /** Full path, unique across the tree; used as the collapse key. */
  path: string;
  folders: FolderNode[];
  items: IndexItem[];
}

function emptyFolder(name: string, path: string): FolderNode {
  return { name, path, folders: [], items: [] };
}

/** Path segments, with blanks and stray slashes dropped. */
export function segments(folder: string): string[] {
  return folder
    .split("/")
    .map((s) => s.trim())
    .filter(Boolean);
}

export function buildFolderTree(items: IndexItem[]): FolderNode {
  const root = emptyFolder("", "");

  for (const item of items) {
    let node = root;
    const parts = segments(item.folder);
    for (const part of parts) {
      const path = node.path ? `${node.path}/${part}` : part;
      let next = node.folders.find((f) => f.name === part);
      if (!next) {
        next = emptyFolder(part, path);
        node.folders.push(next);
      }
      node = next;
    }
    node.items.push(item);
  }

  sort(root);
  return root;
}

function sort(node: FolderNode): void {
  node.folders.sort((a, b) => a.name.localeCompare(b.name));
  // Workspaces first within a folder: a workspace is usually the way in
  // to the forms beside it, so listing it after them buries the door
  // behind the rooms.
  node.items.sort((a, b) =>
    a.kind === b.kind
      ? a.title.localeCompare(b.title)
      : a.kind === "workspace"
        ? -1
        : 1,
  );
  node.folders.forEach(sort);
}

/** Every folder path in the tree, for expand-all / collapse-all. */
export function allFolderPaths(node: FolderNode): string[] {
  return node.folders.flatMap((f) => [f.path, ...allFolderPaths(f)]);
}

/** How many items sit at or below this folder. */
export function countItems(node: FolderNode): number {
  return (
    node.items.length +
    node.folders.reduce((total, f) => total + countItems(f), 0)
  );
}
