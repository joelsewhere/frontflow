/**
 * Checks for chain segmentation.
 *
 *   npx esbuild src/lib/chainSegments.ts --format=cjs --outfile=/tmp/cs.cjs
 *   SEGMENTS_BUNDLE=/tmp/cs.cjs node src/lib/chainSegments.test.cjs
 */
const assert = require("assert");
const { buildChainSegments } = require(
  process.env.SEGMENTS_BUNDLE || "./chainSegments.cjs",
);

let passed = 0;
function check(name, fn) {
  try { fn(); console.log("  ok  " + name); passed++; }
  catch (e) { console.log("  FAIL " + name + "\n       " + e.message); process.exitCode = 1; }
}

const task = (id, kind, extra = {}) => ({
  task_id: id, kind, state: "success", is_hitl: kind === "hitl", ...extra,
});

const ids = (segs) =>
  segs.flatMap((s) => (s.task ? [s.task.task_id] : (s.tasks || []).map((t) => t.task_id)));

console.log("hidden steps:");

check("a hidden task is not drawn", () => {
  const segs = buildChainSegments([
    task("form", "hitl"),
    task("visible_op", "external"),
    task("hidden_op", "external", { hidden: true }),
  ]);
  assert.ok(ids(segs).includes("visible_op"), "the visible operator should draw");
  assert.ok(!ids(segs).includes("hidden_op"), "the hidden operator should not");
});

check("hiding the only operator leaves just the form", () => {
  const segs = buildChainSegments([
    task("form", "hitl"),
    task("hidden_op", "external", { hidden: true }),
  ]);
  assert.deepEqual(ids(segs), ["form"]);
});

check("an unflagged task is drawn as before", () => {
  const segs = buildChainSegments([task("form", "hitl"), task("op", "external")]);
  assert.deepEqual(ids(segs).sort(), ["form", "op"]);
});

check("hiding does not renumber around a failure", () => {
  // A failed operator is never flagged hidden server-side, so it draws.
  const segs = buildChainSegments([
    task("form", "hitl"),
    task("boom", "external", { state: "failed" }),
  ]);
  assert.ok(ids(segs).includes("boom"));
});

console.log(`\n${passed} checks passed`);
