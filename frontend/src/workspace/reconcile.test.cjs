// npx esbuild reconcile.ts --bundle --format=cjs --outfile=reconcile.cjs && node reconcile.test.cjs
const assert = require("assert");
const {
  reconcileLayout,
  panelsInGrid,
  bandFor,
} = require("./reconcile.cjs");

let passed = 0;
const test = (n, f) => { f(); passed += 1; };

const leaf = (...views) => ({
  type: "leaf",
  data: { views, activeView: views[0], id: views.join("-") },
});
const branch = (...children) => ({ type: "branch", data: children });
const layout = (root, panels) => ({
  grid: { root, width: 1000, height: 800, orientation: "HORIZONTAL" },
  panels: panels ?? Object.fromEntries(panelsInGrid({ root }).map((id) => [id, { id }])),
});

const FALLBACK = layout(branch(leaf("a"), leaf("b")));

test("an untouched layout survives unchanged", () => {
  const saved = layout(branch(leaf("b"), leaf("a")));
  const out = reconcileLayout(saved, ["a", "b"], FALLBACK);
  // Order is the arrangement someone chose: b first, not a.
  assert.deepStrictEqual(panelsInGrid(out.grid), ["b", "a"]);
});

test("a removed panel is dropped and its group pruned", () => {
  const saved = layout(branch(leaf("a"), leaf("gone")));
  const out = reconcileLayout(saved, ["a"], FALLBACK);
  assert.deepStrictEqual(panelsInGrid(out.grid), ["a"]);
  // The branch had two children and now has one, so it collapses.
  assert.strictEqual(out.grid.root.type, "leaf");
});

test("a removed panel leaves no state behind", () => {
  const saved = layout(branch(leaf("a"), leaf("gone")));
  const out = reconcileLayout(saved, ["a"], FALLBACK);
  assert.deepStrictEqual(Object.keys(out.panels), ["a"]);
});

test("a newly declared panel is added, arrangement kept", () => {
  const saved = layout(branch(leaf("b"), leaf("a")));
  const out = reconcileLayout(saved, ["a", "b", "c"], FALLBACK);
  const ids = panelsInGrid(out.grid);
  assert.ok(ids.includes("c"), "the new panel must appear");
  // And the existing two keep their dragged order.
  assert.deepStrictEqual(ids.filter((i) => i !== "c"), ["b", "a"]);
});

test("a tabbed group keeps its tabs", () => {
  const saved = layout(branch(leaf("a"), leaf("b", "c")));
  const out = reconcileLayout(saved, ["a", "b", "c"], FALLBACK);
  assert.deepStrictEqual(panelsInGrid(out.grid), ["a", "b", "c"]);
});

test("removing one tab keeps the rest of the group", () => {
  const saved = layout(branch(leaf("a"), leaf("b", "gone", "c")));
  const out = reconcileLayout(saved, ["a", "b", "c"], FALLBACK);
  assert.deepStrictEqual(panelsInGrid(out.grid), ["a", "b", "c"]);
});

test("an active tab that was removed moves to a surviving one", () => {
  // Restoring a grid whose activeView names a panel that is gone is how
  // dockview ends up with a group showing nothing.
  const saved = layout({
    type: "leaf",
    data: { views: ["gone", "b"], activeView: "gone" },
  });
  const out = reconcileLayout(saved, ["b"], FALLBACK);
  assert.strictEqual(out.grid.root.data.activeView, "b");
});

test("nothing recognisable falls back to the declared default", () => {
  // A workspace rewritten from scratch should look like what its author
  // declared, not like a fragment of what it used to be.
  const saved = layout(branch(leaf("old1"), leaf("old2")));
  const out = reconcileLayout(saved, ["a", "b"], FALLBACK);
  assert.strictEqual(out, FALLBACK);
});

test("no saved layout falls back", () => {
  assert.strictEqual(reconcileLayout(null, ["a"], FALLBACK), FALLBACK);
  assert.strictEqual(reconcileLayout(undefined, ["a"], FALLBACK), FALLBACK);
  assert.strictEqual(reconcileLayout({}, ["a"], FALLBACK), FALLBACK);
  assert.strictEqual(reconcileLayout({ grid: {} }, ["a"], FALLBACK), FALLBACK);
});

test("the saved layout is not mutated", () => {
  // It came from the server and may be cached; reconciling twice must
  // give the same answer.
  const saved = layout(branch(leaf("b"), leaf("a")));
  const before = JSON.stringify(saved);
  reconcileLayout(saved, ["a", "b", "c"], FALLBACK);
  assert.strictEqual(JSON.stringify(saved), before);
});

test("reconciling is idempotent", () => {
  const saved = layout(branch(leaf("b"), leaf("a")));
  const once = reconcileLayout(saved, ["a", "b", "c"], FALLBACK);
  const twice = reconcileLayout(once, ["a", "b", "c"], FALLBACK);
  assert.deepStrictEqual(panelsInGrid(twice.grid), panelsInGrid(once.grid));
});

// --- bands ---------------------------------------------------------------

test("width falls into the right band", () => {
  const bps = [900, 1400];
  assert.strictEqual(bandFor(320, bps), 0);
  assert.strictEqual(bandFor(899, bps), 0);
  assert.strictEqual(bandFor(900, bps), 900, "the bound is inclusive");
  assert.strictEqual(bandFor(1399, bps), 900);
  assert.strictEqual(bandFor(1400, bps), 1400);
  assert.strictEqual(bandFor(4000, bps), 1400);
});

test("no declared breakpoints means one band", () => {
  assert.strictEqual(bandFor(320, []), 0);
  assert.strictEqual(bandFor(4000, []), 0);
});

test("unsorted breakpoints still resolve", () => {
  // The DSL refuses these, but a stored value can outlive that rule.
  assert.strictEqual(bandFor(1000, [1400, 900]), 900);
});


// --- preview width and labels -------------------------------------------
{
  const { previewWidthFor, bandLabel, BASE_BAND_PREVIEW } = require("./reconcile.cjs");

  test("a band previews at its floor", () => {
    // Where a layout is tightest is where it breaks. One that works at
    // the floor works everywhere above it; one arranged at the top of a
    // band can fall apart at the bottom.
    assert.strictEqual(previewWidthFor(900), 900);
    assert.strictEqual(previewWidthFor(1400), 1400);
  });

  test("the base band previews at a phone width", () => {
    // Its actual floor is 0, which previews nothing.
    assert.strictEqual(previewWidthFor(0), BASE_BAND_PREVIEW);
  });

  test("labels say what a band covers, not just where it starts", () => {
    const bps = [900, 1400];
    assert.strictEqual(bandLabel(0, bps), "up to 899px");
    assert.strictEqual(bandLabel(900, bps), "900–1399px");
    assert.strictEqual(bandLabel(1400, bps), "1400px and up");
  });

  test("with no breakpoints there is one band covering everything", () => {
    assert.strictEqual(bandLabel(0, []), "all widths");
  });
}



// --- panel state must be rebuilt, never restored -------------------------
{
  const { reconcileLayout } = require("./reconcile.cjs");

  const grid = (root) => ({
    grid: { root, width: 1000, height: 800, orientation: "HORIZONTAL" },
  });
  const oneLeaf = { type: "leaf", data: { views: ["a"], activeView: "a" } };

  test("params come from the fresh build, not the save", () => {
    // A saved layout has been through JSON.stringify and back, so any
    // callback in params is gone. Restoring them would give a panel a
    // dead onMeasure and whatever permissions the author had when they
    // saved.
    const onMeasure = () => 42;
    const fresh = {
      ...grid(oneLeaf),
      panels: { a: { id: "a", params: { onMeasure, canEdit: false } } },
    };
    const stale = {
      ...grid(oneLeaf),
      panels: { a: { id: "a", params: { canEdit: true } } },
    };

    const out = reconcileLayout(stale, ["a"], fresh);
    assert.strictEqual(out.panels.a.params.onMeasure, onMeasure);
    assert.strictEqual(
      out.panels.a.params.canEdit,
      false,
      "permissions must come from this session, not the saved one",
    );
  });

  test("but the ARRANGEMENT still comes from the save", () => {
    // The whole point: structure is theirs, state is ours.
    const fresh = {
      ...grid({ type: "branch", data: [
        { type: "leaf", data: { views: ["a"] } },
        { type: "leaf", data: { views: ["b"] } },
      ] }),
      panels: { a: { id: "a" }, b: { id: "b" } },
    };
    const saved = {
      ...grid({ type: "leaf", data: { views: ["b", "a"], activeView: "b" } }),
      panels: { a: { id: "a" }, b: { id: "b" } },
    };

    const out = reconcileLayout(saved, ["a", "b"], fresh);
    assert.strictEqual(out.grid.root.type, "leaf", "tabbed, as saved");
    assert.deepStrictEqual(out.grid.root.data.views, ["b", "a"]);
  });
}


console.log(`reconcile: ${passed} tests passed`);
