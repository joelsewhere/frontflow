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


// --- several tabs sharing one rail --------------------------------------
{
  const {
    applyRailShape,
    releaseRailShape,
    railShapeMutations,
  } = require("./railShape.cjs");

  /** One group, one shared strip, N tabs inside it. */
  function railWith(tabCount) {
    const tabs = Array.from(
      { length: tabCount },
      (_, i) =>
        `<div class="dv-tab"><div class="dockview-react-part">
           <div id="own${i}"></div></div></div>`,
    ).join("");
    const dom = new JSDOM(`
      <div class="dv-groupview">
        <div class="dv-tabs-and-actions-container">
          <div class="dv-tabs-container">${tabs}</div>
        </div>
      </div>`);
    const d = dom.window.document;
    return {
      d,
      strip: d.querySelector(".dv-tabs-container"),
      owns: Array.from({ length: tabCount }, (_, i) => d.getElementById(`own${i}`)),
    };
  }

  test("one tab shapes and unshapes the strip", () => {
    const { strip, owns } = railWith(1);
    const owner = {};
    applyRailShape(owner, railShapeMutations(owns[0]));
    assert.strictEqual(strip.style.flexDirection, "column");
    releaseRailShape(owner);
    assert.strictEqual(strip.style.flexDirection, "");
  });

  test("SEVERAL tabs unshape it too", () => {
    // The reported bug. Each tab used to record the value it found and
    // put that back, so the second tab recorded the FIRST tab's rail
    // styling and restored it — the strip came back from an expand
    // still stacked, with the tabs and content unusable. A rail with
    // one tab worked, which is why it only appeared after panels were
    // dragged in.
    const { strip, owns } = railWith(7);
    const owners = owns.map(() => ({}));
    owns.forEach((own, i) =>
      applyRailShape(owners[i], railShapeMutations(own)),
    );
    assert.strictEqual(strip.style.flexDirection, "column");

    owners.forEach(releaseRailShape);
    assert.strictEqual(
      strip.style.flexDirection,
      "",
      "the strip must be unshaped once every tab has let go",
    );
  });

  test("the strip stays shaped until the LAST tab lets go", () => {
    const { strip, owns } = railWith(3);
    const owners = owns.map(() => ({}));
    owns.forEach((own, i) => applyRailShape(owners[i], railShapeMutations(own)));

    releaseRailShape(owners[0]);
    assert.strictEqual(strip.style.flexDirection, "column", "still collapsed");
    releaseRailShape(owners[1]);
    assert.strictEqual(strip.style.flexDirection, "column", "still collapsed");
    releaseRailShape(owners[2]);
    assert.strictEqual(strip.style.flexDirection, "");
  });

  test("a pre-existing inline value is preserved, not invented", () => {
    // .dockview-react-part is born with width:100%; removing the
    // property outright would take dockview's own styling with it.
    const { d, owns } = railWith(2);
    const part = d.querySelector(".dockview-react-part");
    part.style.setProperty("width", "50%");

    const owners = [{}, {}];
    owns.forEach((own, i) => applyRailShape(owners[i], railShapeMutations(own)));
    owners.forEach(releaseRailShape);

    assert.strictEqual(part.style.width, "50%");
  });

  test("re-applying does not compound", () => {
    // A MutationObserver re-runs this on every childList change.
    const { strip, owns } = railWith(2);
    const owners = [{}, {}];
    for (let round = 0; round < 5; round += 1) {
      owns.forEach((own, i) => applyRailShape(owners[i], railShapeMutations(own)));
    }
    owners.forEach(releaseRailShape);
    assert.strictEqual(strip.style.flexDirection, "");
  });

  test("releasing something never applied is harmless", () => {
    releaseRailShape({});
  });
}


console.log(`railShape: ${passed} tests passed`);
