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


console.log(`storyHtml: ${passed} tests passed`);
