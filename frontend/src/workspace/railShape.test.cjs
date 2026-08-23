// npx esbuild railShape.ts --bundle --format=cjs --outfile=railShape.cjs && node railShape.test.cjs
const assert = require("assert");
const { JSDOM } = require("jsdom");
const { railShapeMutations, groupOf } = require("./railShape.cjs");

let passed = 0;
const test = (n, f) => { f(); passed += 1; };

/** dockview's real nesting, tab -> group. */
function tree() {
  const dom = new JSDOM(`
    <div class="dv-groupview">
      <div class="dv-tabs-and-actions-container dv-single-tab">
        <div class="dv-tabs-container">
          <div class="dv-tab dv-active-tab">
            <div class="dockview-react-part">
              <div id="own" class="handle"></div>
            </div>
          </div>
        </div>
      </div>
    </div>`);
  const d = dom.window.document;
  return { d, own: d.getElementById("own") };
}

const styleFor = (muts, el) =>
  Object.fromEntries(
    muts.filter((m) => m.element === el).map((m) => [m.property, m.value]),
  );

test("every ancestor up to the group is spanned", () => {
  // Centring only means something if the WHOLE chain spans the rail:
  // one wrapper at its natural width and the handle centres inside
  // that instead, which looks like no centring at all.
  const { d, own } = tree();
  const muts = railShapeMutations(own);
  for (const sel of [
    "#own",
    ".dockview-react-part",
    ".dv-tab",
    ".dv-tabs-container",
    ".dv-tabs-and-actions-container",
  ]) {
    assert.strictEqual(
      styleFor(muts, d.querySelector(sel)).width,
      "100%",
      `${sel} must span the rail`,
    );
  }
});

test("the tab's min-width is defeated", () => {
  // dockview gives .dv-tab min-width:75px, wider than the 44px rail, so
  // the handle centred in 75px rather than in what is on screen. This
  // is the exact bug an ancestor-width walk diagnosed.
  const { d, own } = tree();
  const tab = styleFor(railShapeMutations(own), d.querySelector(".dv-tab"));
  assert.strictEqual(tab["min-width"], "0");
  assert.strictEqual(tab.padding, "0");
});

test("the strip stacks and stops clipping", () => {
  const { d, own } = tree();
  const muts = railShapeMutations(own);
  const container = styleFor(muts, d.querySelector(".dv-tabs-container"));
  assert.strictEqual(container["flex-direction"], "column");
  assert.strictEqual(container.overflow, "visible");
  const outer = styleFor(muts, d.querySelector(".dv-tabs-and-actions-container"));
  assert.strictEqual(outer.height, "100%");
  assert.strictEqual(outer["flex-direction"], "column");
});

test("the walk stops at the group", () => {
  // Styling the group itself would resize the whole panel, not the rail.
  //
  // Deliberately WITHOUT a dv-tabs-and-actions-container: in the normal
  // tree the walk breaks there, one level below the group, so the group
  // is never reached and this guard is never exercised. Removing it
  // passed every other test here.
  const dom = new JSDOM(`
    <div class="dv-groupview">
      <div class="wrapper">
        <div id="own"></div>
      </div>
    </div>`);
  const d2 = dom.window.document;
  const own2 = d2.getElementById("own");
  const muts = railShapeMutations(own2);

  assert.deepStrictEqual(
    styleFor(muts, d2.querySelector(".dv-groupview")),
    {},
    "the group itself must never be styled",
  );
  // The wrapper below it still is.
  assert.strictEqual(styleFor(muts, d2.querySelector(".wrapper")).width, "100%");
});

test("an unknown wrapper is still spanned", () => {
  // The walk names three classes but must not depend on them: dockview
  // is free to add a wrapper, and a missed one breaks centring.
  const dom = new JSDOM(`
    <div class="dv-groupview">
      <div class="dv-tabs-and-actions-container">
        <div class="dv-tabs-container">
          <div class="dv-tab">
            <div class="some-future-wrapper">
              <div id="own"></div>
            </div>
          </div>
        </div>
      </div>
    </div>`);
  const d = dom.window.document;
  const muts = railShapeMutations(d.getElementById("own"));
  assert.strictEqual(
    styleFor(muts, d.querySelector(".some-future-wrapper")).width,
    "100%",
  );
});

test("a detached tab yields only its own width", () => {
  // Mid-drag a tab can have no group above it. It must not throw.
  const dom = new JSDOM(`<div id="own"></div>`);
  const own = dom.window.document.getElementById("own");
  const muts = railShapeMutations(own);
  assert.deepStrictEqual(muts.map((m) => m.property), ["width"]);
  assert.strictEqual(groupOf(own), null);
});

test("groupOf finds the group to observe", () => {
  // This is what the re-parent fix watches. Without a group there is
  // nothing to observe, and a dropped tab stays uncentred.
  const { d, own } = tree();
  assert.strictEqual(groupOf(own), d.querySelector(".dv-groupview"));
});

test("re-parenting changes which elements are shaped", () => {
  // The actual bug: styles were applied to the ancestors the tab HAD.
  // After a drop the chain is different, so the mutation set must be
  // different — which is why the walk has to run again.
  const { d, own } = tree();
  const before = railShapeMutations(own).map((m) => m.element);

  const other = d.createElement("div");
  other.className = "dv-groupview";
  const strip = d.createElement("div");
  strip.className = "dv-tabs-and-actions-container";
  const container = d.createElement("div");
  container.className = "dv-tabs-container";
  const tab = d.createElement("div");
  tab.className = "dv-tab";
  tab.appendChild(own);
  container.appendChild(tab);
  strip.appendChild(container);
  other.appendChild(strip);
  d.body.appendChild(other);

  const after = railShapeMutations(own).map((m) => m.element);
  assert.notDeepStrictEqual(before, after);
  assert.strictEqual(groupOf(own), other);
  assert.strictEqual(styleFor(railShapeMutations(own), tab)["min-width"], "0");
});

console.log(`railShape: ${passed} tests passed`);
