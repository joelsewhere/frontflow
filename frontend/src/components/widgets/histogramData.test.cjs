/**
 * Checks for histogram bar ordering.
 *
 *   npx esbuild src/components/widgets/histogramData.ts --format=cjs --outfile=/tmp/h.cjs
 *   HISTOGRAM_BUNDLE=/tmp/h.cjs node src/components/widgets/histogramData.test.cjs
 */
const assert = require("assert");
const { orderHistogramData } = require(
  process.env.HISTOGRAM_BUNDLE || "./histogramData.cjs",
);

let passed = 0;
function check(name, fn) {
  try { fn(); console.log("  ok  " + name); passed++; }
  catch (e) { console.log("  FAIL " + name + "\n       " + e.message); process.exitCode = 1; }
}

const labels = (d) => d.map((x) => x.label);

console.log("ordering:");

check("a numeric axis with negatives is ordered ascending", () => {
  // The reported bug: selecting the whole range produced
  // `>= 2 AND <= -200`, which matches nothing.
  const raw = { "-2000": 1, "-200": 1, "2": 2, "700000": 1 };
  assert.deepEqual(labels(orderHistogramData(raw)), ["-2000", "-200", "2", "700000"]);
});

check("JS key order really is the problem, not the test's setup", () => {
  const raw = { "-2000": 1, "-200": 1, "2": 2, "700000": 1 };
  // Guards against this test passing because the input happened to be
  // ordered already.
  assert.deepEqual(Object.keys(raw), ["2", "700000", "-2000", "-200"]);
});

check("the full range spans smallest to largest", () => {
  const d = orderHistogramData({ "-2000": 1, "900": 1, "2": 1 });
  assert.equal(d[0].label, "-2000");
  assert.equal(d[d.length - 1].label, "900");
});

check("all-positive data is ordered numerically, not lexically", () => {
  // Lexical order would put "10" before "9".
  assert.deepEqual(labels(orderHistogramData({ "9": 1, "10": 1, "100": 1 })),
    ["9", "10", "100"]);
});

check("decimals order correctly", () => {
  assert.deepEqual(labels(orderHistogramData({ "1.5": 1, "-0.5": 1, "10.25": 1 })),
    ["-0.5", "1.5", "10.25"]);
});

check("ISO dates keep the order they arrived in", () => {
  // Date keys are not integer-like, so JS preserves insertion order and
  // the caller's order is meaningful — reordering could only break it.
  const raw = { "2025-01-06": 3, "2025-01-13": 5, "2025-01-20": 2 };
  assert.deepEqual(labels(orderHistogramData(raw)),
    ["2025-01-06", "2025-01-13", "2025-01-20"]);
});

check("categories are left alone", () => {
  const raw = { north: 3, south: 1, east: 2 };
  assert.deepEqual(labels(orderHistogramData(raw)), ["north", "south", "east"]);
});

check("empty and missing data are handled", () => {
  assert.deepEqual(orderHistogramData(undefined), []);
  assert.deepEqual(orderHistogramData({}), []);
});

console.log(`\n${passed} checks passed`);
