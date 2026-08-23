/**
 * Checks for the index's folder tree.
 *
 *   npx esbuild src/lib/folderTree.ts --format=cjs --outfile=/tmp/ft.cjs
 *   TREE_BUNDLE=/tmp/ft.cjs node src/lib/folderTree.test.cjs
 */
const assert = require("assert");
const { buildFolderTree, allFolderPaths, countItems, segments } = require(
  process.env.TREE_BUNDLE || "./folderTree.cjs",
);

let passed = 0;
function check(name, fn) {
  try { fn(); console.log("  ok  " + name); passed++; }
  catch (e) { console.log("  FAIL " + name + "\n       " + e.message); process.exitCode = 1; }
}

const form = (id, folder, title) => ({ kind: "form", id, folder, title: title || id });
const ws = (id, folder, title) => ({ kind: "workspace", id, folder, title: title || id });

const names = (node) => node.folders.map((f) => f.name);
const ids = (node) => node.items.map((i) => i.id);

console.log("shape:");

check("root-level items stay at the root", () => {
  const t = buildFolderTree([form("a", ""), form("b", "")]);
  assert.deepEqual(ids(t), ["a", "b"]);
  assert.deepEqual(names(t), []);
});

check("a path nests by segment, not by string", () => {
  const t = buildFolderTree([form("a", "sales/intake")]);
  assert.deepEqual(names(t), ["sales"]);
  assert.deepEqual(names(t.folders[0]), ["intake"]);
  assert.deepEqual(ids(t.folders[0].folders[0]), ["a"]);
});

check("siblings share a parent rather than duplicating it", () => {
  const t = buildFolderTree([form("a", "sales/intake"), form("b", "sales/reports")]);
  assert.deepEqual(names(t), ["sales"]);
  assert.deepEqual(names(t.folders[0]), ["intake", "reports"]);
});

check("forms and workspaces share a folder", () => {
  // The point of shelving by source folder: a folder holds whatever was
  // declared in it.
  const t = buildFolderTree([form("f", "sales"), ws("w", "sales")]);
  assert.deepEqual(ids(t.folders[0]).sort(), ["f", "w"]);
});

check("workspaces list before forms in a folder", () => {
  const t = buildFolderTree([form("f", "sales"), ws("w", "sales")]);
  assert.deepEqual(ids(t.folders[0]), ["w", "f"]);
});

check("folders and items are sorted", () => {
  const t = buildFolderTree([form("b", "z"), form("a", "a"), form("c", "a")]);
  assert.deepEqual(names(t), ["a", "z"]);
  assert.deepEqual(ids(t.folders[0]), ["a", "c"]);
});

console.log("paths:");

check("stray slashes and blanks do not create folders", () => {
  const t = buildFolderTree([form("a", "/sales//intake/")]);
  assert.deepEqual(names(t), ["sales"]);
  assert.deepEqual(names(t.folders[0]), ["intake"]);
  assert.deepEqual(segments("//a// b //"), ["a", "b"]);
});

check("folder paths are full, so they are unique collapse keys", () => {
  // Two folders named "intake" under different parents must not share a
  // key, or collapsing one would collapse both.
  const t = buildFolderTree([form("a", "sales/intake"), form("b", "ops/intake")]);
  assert.deepEqual(allFolderPaths(t).sort(),
    ["ops", "ops/intake", "sales", "sales/intake"]);
});

check("counting includes nested items", () => {
  const t = buildFolderTree([
    form("a", "sales"), form("b", "sales/intake"), form("c", "sales/intake/deep"),
  ]);
  assert.equal(countItems(t), 3);
  assert.equal(countItems(t.folders[0]), 3);
  assert.equal(countItems(t.folders[0].folders[0]), 2);
});

check("an empty index is a bare root", () => {
  const t = buildFolderTree([]);
  assert.deepEqual(names(t), []);
  assert.deepEqual(ids(t), []);
  assert.equal(countItems(t), 0);
});

console.log(`\n${passed} checks passed`);
