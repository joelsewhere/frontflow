/**
 * Checks for dashboard directive matching and translation.
 *
 *   npx esbuild src/components/blocks/dashboardDirectives.ts --format=cjs --outfile=/tmp/d.cjs
 *   DIRECTIVES_BUNDLE=/tmp/d.cjs node src/components/blocks/dashboardDirectives.test.cjs
 */
const assert = require("assert");
const { latestFilterDirectiveFor, buildFilterMask } = require(
  process.env.DIRECTIVES_BUNDLE || "./dashboardDirectives.cjs",
);

let passed = 0;
function check(name, fn) {
  try { fn(); console.log("  ok  " + name); passed++; }
  catch (e) { console.log("  FAIL " + name + "\n       " + e.message); process.exitCode = 1; }
}

const task = (dashboard, panel, token, filters = { Region: "East" }) => ({
  dashboard_filters: { dashboard, panel, token, filters },
});

console.log("which panel a directive addresses:");

check("a named panel reaches only that panel", () => {
  const tasks = [task("sales_overview", "detail", "t1")];
  assert.ok(latestFilterDirectiveFor(tasks, "sales_overview", "detail"));
  assert.equal(
    latestFilterDirectiveFor(tasks, "sales_overview", "dashboard-sales_overview"),
    null,
    "the other rendering of the same dashboard must be left alone",
  );
});

check("an unnamed panel reaches every rendering", () => {
  const tasks = [task("sales_overview", null, "t1")];
  assert.ok(latestFilterDirectiveFor(tasks, "sales_overview", "detail"));
  assert.ok(latestFilterDirectiveFor(tasks, "sales_overview", "anything-else"));
});

check("a different dashboard is never touched", () => {
  const tasks = [task("sales_overview", null, "t1")];
  assert.equal(latestFilterDirectiveFor(tasks, "other_dashboard", "detail"), null);
});

check("the newest token wins", () => {
  const tasks = [
    task("sales_overview", "detail", "2026-01-01T00:00:01", { Region: "North" }),
    task("sales_overview", "detail", "2026-01-01T00:00:09", { Region: "South" }),
  ];
  const got = latestFilterDirectiveFor(tasks, "sales_overview", "detail");
  assert.equal(got.filters.Region, "South");
});

console.log("named filters as a Superset data mask:");

const filters = [
  { id: "F1", name: "Region", column: "Region", filter_type: "filter_select", is_time: false },
  { id: "F2", name: "Live refresh", column: "created_at", filter_type: "filter_time", is_time: true },
];

check("a value filter becomes an IN clause on its column", () => {
  const mask = buildFilterMask({ Region: "East" }, filters);
  assert.deepEqual(mask.F1.extraFormData.filters, [
    { col: "Region", op: "IN", val: ["East"] },
  ]);
  assert.deepEqual(mask.F1.filterState.value, ["East"]);
});

check("matching the filter name ignores case", () => {
  assert.ok(buildFilterMask({ region: "East" }, filters).F1, "region -> Region");
});

check("a list value selects several", () => {
  const mask = buildFilterMask({ Region: ["East", "West"] }, filters);
  assert.deepEqual(mask.F1.extraFormData.filters[0].val, ["East", "West"]);
});

check("a time filter takes a range, not an IN clause", () => {
  const mask = buildFilterMask({ "Live refresh": " : 2026-01-01T00:00:00" }, filters);
  assert.equal(mask.F2.extraFormData.time_range, " : 2026-01-01T00:00:00");
  assert.ok(!mask.F2.extraFormData.filters, "a time filter must not emit IN");
});

check("a filter the dashboard does not have is skipped, not guessed at", () => {
  assert.deepEqual(buildFilterMask({ Nope: "x" }, filters), {});
});

console.log(`\n${passed} checks passed`);

// --- range filters --------------------------------------------------------
console.log("range filters:");

const withRange = filters.concat([
  { id: "F3", name: "Units", column: "units", filter_type: "filter_range", is_time: false },
]);

check("a range filter becomes two bounds, not an IN clause", () => {
  const mask = buildFilterMask({ Units: ["10", "500"] }, withRange);
  assert.deepEqual(mask.F3.extraFormData.filters, [
    { col: "units", op: ">=", val: 10 },
    { col: "units", op: "<=", val: 500 },
  ]);
  assert.deepEqual(mask.F3.filterState.value, [10, 500]);
});

check("the filter's TYPE decides, not the shape of the value", () => {
  // The identical pair on a value filter means two selections.
  const mask = buildFilterMask({ Region: ["East", "West"] }, withRange);
  assert.equal(mask.F1.extraFormData.filters[0].op, "IN");
  assert.deepEqual(mask.F1.extraFormData.filters[0].val, ["East", "West"]);
});

check("a non-numeric bound is skipped rather than sent", () => {
  assert.deepEqual(buildFilterMask({ Units: ["", "500"] }, withRange), {});
  assert.deepEqual(buildFilterMask({ Units: ["abc", "9"] }, withRange), {});
});

check("negative and zero bounds are honoured", () => {
  // `0` is falsy — a truthiness check here would silently drop it.
  const mask = buildFilterMask({ Units: ["-2000", "0"] }, withRange);
  assert.deepEqual(mask.F3.extraFormData.filters, [
    { col: "units", op: ">=", val: -2000 },
    { col: "units", op: "<=", val: 0 },
  ]);
});

console.log(`\n${passed} checks passed`);
