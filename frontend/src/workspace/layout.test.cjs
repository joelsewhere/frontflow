/**
 * Checks for the pure layout builder.
 *
 * The project has no frontend test runner, and adding one for this is
 * more than it warrants — but this module is the part of the workspace
 * with real structure in it, and it is pure, so it can be transpiled
 * and run directly:
 *
 *   npx esbuild src/workspace/layout.ts --format=cjs --outfile=/tmp/layout.cjs
 *   LAYOUT_BUNDLE=/tmp/layout.cjs node src/workspace/layout.test.cjs
 */
const assert = require("assert");
const { normalize, buildDockLayout } = require(process.env.LAYOUT_BUNDLE || "./layout.cjs");

const panel = (type, id, props = {}) => ({ type, id, props, children: [] });
const box = (type, ...children) => ({ type, id: null, props: {}, children });

const form = panel("workspace_form", "form-sales");
const dash = panel("dashboard", "dash-a");
const explore = panel("superset_explore", "explore-v");
const detail = panel("dashboard", "detail");

let passed = 0;
function check(name, fn) {
  try { fn(); console.log("  ok  " + name); passed++; }
  catch (e) { console.log("  FAIL " + name + "\n       " + e.message); process.exitCode = 1; }
}

console.log("normalize:");

check("a Row becomes a HORIZONTAL branch", () => {
  const n = normalize(box("row", form, dash));
  assert.equal(n.type, "branch");
  assert.equal(n.orientation, "HORIZONTAL");
  assert.equal(n.children.length, 2);
});

check("a Column becomes a VERTICAL branch", () => {
  assert.equal(normalize(box("column", form, dash)).orientation, "VERTICAL");
});

check("tabs collapse into ONE leaf", () => {
  const n = normalize(box("tabs", dash, explore));
  assert.equal(n.type, "leaf");
  assert.equal(n.panels.length, 2);
});

check("Column in Column is flattened (grid alternates by depth)", () => {
  const n = normalize(box("column", box("column", form, dash), detail));
  assert.equal(n.orientation, "VERTICAL");
  assert.equal(n.children.length, 3, "expected 3 flattened children");
  assert.ok(n.children.every((c) => c.type === "leaf"));
});

check("Row inside Column is preserved as a nested branch", () => {
  const n = normalize(box("column", box("row", form, dash), detail));
  assert.equal(n.orientation, "VERTICAL");
  assert.equal(n.children.length, 2);
  assert.equal(n.children[0].type, "branch");
  assert.equal(n.children[0].orientation, "HORIZONTAL");
});

check("a container holding one thing collapses away", () => {
  assert.equal(normalize(box("column", box("row", form))).type, "leaf");
});

check("after normalize, nested branches always alternate", () => {
  const n = normalize(box("column", box("row", form, box("column", dash, detail)), explore));
  const walk = (node, expected) => {
    if (node.type !== "branch") return;
    assert.equal(node.orientation, expected, "orientation must alternate by depth");
    node.children.forEach((c) => walk(c, expected === "HORIZONTAL" ? "VERTICAL" : "HORIZONTAL"));
  };
  walk(n, n.orientation);
});

console.log("buildDockLayout:");

const state = (b, id) => ({ id, contentComponent: "x", title: id });

check("the real failing case: Column(Row(form, tabs), dashboard)", () => {
  const tree = box("column", box("row", form, box("tabs", dash, explore)), detail);
  const out = buildDockLayout(tree, { width: 1000, height: 900 }, state);
  assert.equal(out.grid.orientation, "VERTICAL", "root must stack");
  const rootKids = out.grid.root.data;
  assert.equal(rootKids.length, 2);
  assert.equal(rootKids[0].type, "branch", "the Row must survive as a branch");
  assert.equal(rootKids[1].type, "leaf", "the trailing dashboard is its own row");
  // The row's two children sit side by side.
  assert.equal(rootKids[0].data.length, 2);
  assert.deepEqual(rootKids[0].data[1].data.views, ["dash-a", "explore-v"]);
});

check("every panel gets a state entry", () => {
  const tree = box("column", box("row", form, box("tabs", dash, explore)), detail);
  const out = buildDockLayout(tree, { width: 1000, height: 900 }, state);
  assert.deepEqual(Object.keys(out.panels).sort(), ["dash-a", "detail", "explore-v", "form-sales"].sort());
});

check("sibling sizes add up to the parent's extent", () => {
  const tree = box("column", form, dash, detail);
  const out = buildDockLayout(tree, { width: 1000, height: 900 }, state);
  const total = out.grid.root.data.reduce((s, c) => s + c.size, 0);
  assert.equal(total, 900, "vertical children must fill the height exactly");
});

check("a declared min_height claims proportionally more room", () => {
  const tall = panel("dashboard", "tall", { min_height: 800 });
  const short = panel("dashboard", "short", { min_height: 200 });
  const out = buildDockLayout(box("column", tall, short), { width: 1000, height: 1000 }, state);
  const [a, b] = out.grid.root.data;
  assert.ok(a.size > b.size * 3, `expected the taller panel to dominate, got ${a.size} vs ${b.size}`);
});

check("measured heights override declared ones", () => {
  const p = panel("workspace_form", "f", { min_height: 100 });
  const q = panel("dashboard", "d", { min_height: 100 });
  const out = buildDockLayout(box("column", p, q), { width: 1000, height: 1000 }, state, { f: 900 });
  const [a, b] = out.grid.root.data;
  assert.ok(a.size > b.size, `measured 900 should beat declared 100, got ${a.size} vs ${b.size}`);
});

console.log(`\n${passed} checks passed`);

// --- requiredHeightForGrid ------------------------------------------------
const { requiredHeightForGrid, GROUP_CHROME_PX } = require(process.env.LAYOUT_BUNDLE || "./layout.cjs");

check("a Row splits WIDTH evenly, ignoring declared heights", () => {
  // The load-time bug: the form has no measured height yet, so a
  // height-weighted width split gave it a sliver beside a dashboard
  // declaring min_height=560.
  const f = panel("workspace_form", "f", {});
  const d = panel("dashboard", "d", { min_height: 560 });
  const out = buildDockLayout(box("row", f, d), { width: 1000, height: 800 }, state);
  const [a, b] = out.grid.root.data;
  assert.equal(a.size, b.size, `expected an even width split, got ${a.size} vs ${b.size}`);
});

check("a Column still honours heights", () => {
  const tall = panel("dashboard", "tall", { min_height: 800 });
  const short = panel("dashboard", "short", { min_height: 200 });
  const out = buildDockLayout(box("column", tall, short), { width: 1000, height: 1000 }, state);
  const [a, b] = out.grid.root.data;
  assert.ok(a.size > b.size * 3, `heights must still drive a Column, got ${a.size} vs ${b.size}`);
});

console.log("requiredHeightForGrid:");

const leaf = (...views) => ({ type: "leaf", size: 0, data: { id: "g", views } });
const branch = (...kids) => ({ type: "branch", size: 0, data: kids });
const floors = { form: 1400, tabs: 560, detail: 520 };
const floorOf = (id) => floors[id] ?? 0;

check("declared shape: Column(Row(form, tabs), detail)", () => {
  // root VERTICAL; its first child is a HORIZONTAL branch.
  const grid = branch(branch(leaf("form"), leaf("tabs")), leaf("detail"));
  const h = requiredHeightForGrid(grid, "VERTICAL", floorOf);
  // max(1400, 560) + 520, each group plus its own tab strip
  assert.equal(h, 1400 + GROUP_CHROME_PX + 520 + GROUP_CHROME_PX);
});

check("after dragging the form out into its own row it needs MORE", () => {
  const grid = branch(leaf("form"), leaf("tabs"), leaf("detail"));
  const h = requiredHeightForGrid(grid, "VERTICAL", floorOf);
  assert.equal(h, 1400 + 560 + 520 + 3 * GROUP_CHROME_PX);
});

check("the rearranged layout is taller than the declared one", () => {
  const declared = requiredHeightForGrid(
    branch(branch(leaf("form"), leaf("tabs")), leaf("detail")), "VERTICAL", floorOf);
  const dragged = requiredHeightForGrid(
    branch(leaf("form"), leaf("tabs"), leaf("detail")), "VERTICAL", floorOf);
  assert.ok(dragged > declared, "this gap is the bug: the canvas was sized to the smaller one");
});

check("tabbed panels share one region", () => {
  const grid = branch(leaf("tabs", "detail"));
  assert.equal(requiredHeightForGrid(grid, "VERTICAL", floorOf), 560 + GROUP_CHROME_PX);
});

check("side-by-side groups take only the tallest", () => {
  const grid = branch(leaf("form"), leaf("detail"));
  assert.equal(requiredHeightForGrid(grid, "HORIZONTAL", floorOf), 1400 + GROUP_CHROME_PX);
});

console.log("\nall checks passed");
