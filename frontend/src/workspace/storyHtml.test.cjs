// npx esbuild storyHtml.ts --bundle --format=cjs --outfile=storyHtml.cjs && node storyHtml.test.cjs
const assert = require("assert");
const { storyState, storyNotice } = require("./storyHtml.cjs");

let passed = 0;
const test = (n, f) => { f(); passed += 1; };

test("an unrendered story is 'missing'", () => {
  assert.strictEqual(storyState({ rendered: false, html: null }), "missing");
  assert.strictEqual(storyState({ rendered: true, html: "" }), "missing");
  assert.strictEqual(storyState({ rendered: true, html: null }), "missing");
});

test("a rendered story is 'ready'", () => {
  assert.strictEqual(storyState({ rendered: true, html: "<p>x</p>" }), "ready");
});

test("failing cells outrank staleness", () => {
  // Both true: the broken render is the more urgent thing to say.
  const n = storyNotice({ stale: true, cell_errors: 2 });
  assert.strictEqual(n.tone, "error");
  assert.ok(n.text.includes("2 cells"));
});

test("one failing cell reads as singular", () => {
  assert.strictEqual(
    storyNotice({ cell_errors: 1 }).text,
    "1 cell failed when this was rendered.",
  );
});

test("a stale story warns", () => {
  const n = storyNotice({ stale: true });
  assert.strictEqual(n.tone, "warning");
  assert.ok(n.text.includes("older version"));
});

test("a current story says nothing", () => {
  assert.strictEqual(storyNotice({ stale: false, cell_errors: 0 }), null);
});

test("unknown staleness is not reported as stale", () => {
  // null means "no header, cannot tell" — claiming either way is wrong.
  assert.strictEqual(storyNotice({ stale: null }), null);
  assert.strictEqual(storyNotice({}), null);
});



// --- height messages -----------------------------------------------------
{
  const { storyHeightFrom, STORY_HEIGHT_MESSAGE } = require("./storyHtml.cjs");

  test("a well-formed height message is read", () => {
    assert.strictEqual(
      storyHeightFrom({ type: STORY_HEIGHT_MESSAGE, height: 820 }),
      820,
    );
  });

  test("anything else is ignored", () => {
    // The window carries other traffic — dockview, the Superset embed
    // SDK, browser extensions.
    for (const junk of [
      null,
      undefined,
      "frontflow:story-height",
      42,
      {},
      { type: "other", height: 100 },
      { type: STORY_HEIGHT_MESSAGE },
      { type: STORY_HEIGHT_MESSAGE, height: "820" },
    ]) {
      assert.strictEqual(storyHeightFrom(junk), null, JSON.stringify(junk));
    }
  });

  test("nonsense heights are refused", () => {
    // A zero or NaN height applied to a panel collapses it.
    for (const h of [0, -10, NaN, Infinity]) {
      assert.strictEqual(
        storyHeightFrom({ type: STORY_HEIGHT_MESSAGE, height: h }),
        null,
        String(h),
      );
    }
  });

  test("shape alone never identifies the SENDER", () => {
    // Two stories post identical messages. Nothing here distinguishes
    // them, which is why the panel must compare event.source — without
    // it, every story applied every other story's height and one tall
    // story pushed its neighbours over the panels below.
    const a = { type: STORY_HEIGHT_MESSAGE, height: 2000 };
    const b = { type: STORY_HEIGHT_MESSAGE, height: 300 };
    assert.strictEqual(storyHeightFrom(a), 2000);
    assert.strictEqual(storyHeightFrom(b), 300);
  });
}

console.log(`storyHtml: ${passed} tests passed`);
