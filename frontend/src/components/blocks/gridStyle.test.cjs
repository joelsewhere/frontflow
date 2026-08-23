// Run: npx esbuild gridStyle.ts --bundle --format=cjs --outfile=gridStyle.cjs && node gridStyle.test.cjs
const assert = require("assert");
const {
  alignClassFor,
  columnsFor,
  gridStyleFor,
  cellStyleFor,
  gridItemStyleFor,
  GRID_MAX_COLUMNS,
} = require("./gridStyle.cjs");

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
}

test("align maps to literal classes", () => {
  assert.strictEqual(alignClassFor("end"), "items-end");
  assert.strictEqual(alignClassFor("start"), "items-start");
  assert.strictEqual(alignClassFor("center"), "items-center");
  assert.strictEqual(alignClassFor("stretch"), "items-stretch");
});

test("unknown or missing align falls back to stretch", () => {
  assert.strictEqual(alignClassFor("sideways"), "items-stretch");
  assert.strictEqual(alignClassFor(undefined), "items-stretch");
  assert.strictEqual(alignClassFor(null), "items-stretch");
  assert.strictEqual(alignClassFor(4), "items-stretch");
});

test("no class is ever built by interpolation", () => {
  // The whole point: every value the function can return has to be a
  // string Tailwind could have found by scanning this file.
  const emitted = ["start", "center", "end", "stretch", "nonsense"].map(
    alignClassFor,
  );
  const literalsInSource = [
    "items-start",
    "items-center",
    "items-end",
    "items-stretch",
  ];
  for (const cls of emitted) {
    assert.ok(
      literalsInSource.includes(cls),
      `${cls} is not one of the literals Tailwind can see`,
    );
  }
});

test("columns clamp into range", () => {
  assert.strictEqual(columnsFor(4), 4);
  assert.strictEqual(columnsFor(0), 1);
  assert.strictEqual(columnsFor(-3), 1);
  assert.strictEqual(columnsFor(99), GRID_MAX_COLUMNS);
  assert.strictEqual(columnsFor(2.7), 2);
});

test("a missing or junk column count degrades to one column", () => {
  // An older compiled graph can predate the validator.
  assert.strictEqual(columnsFor(undefined), 1);
  assert.strictEqual(columnsFor("4"), 1);
  assert.strictEqual(columnsFor(NaN), 1);
  assert.strictEqual(columnsFor(Infinity), 1);
});

test("grid style carries the count as a custom property", () => {
  assert.deepStrictEqual(gridStyleFor(4), { "--ff-grid-cols": "4" });
  assert.deepStrictEqual(gridStyleFor(undefined), { "--ff-grid-cols": "1" });
});

test("span 1 emits no style at all", () => {
  assert.strictEqual(cellStyleFor(1), undefined);
  assert.strictEqual(cellStyleFor(undefined), undefined);
  assert.strictEqual(cellStyleFor(0), undefined);
});

test("a real span emits both halves of the shorthand", () => {
  // `grid-column: span 2` alone sets start-span only in some engines;
  // the two-value form is unambiguous.
  assert.deepStrictEqual(cellStyleFor(2), {
    gridColumn: "span 2 / span 2",
  });
  assert.deepStrictEqual(cellStyleFor(99), {
    gridColumn: `span ${GRID_MAX_COLUMNS} / span ${GRID_MAX_COLUMNS}`,
  });
});


// --- the span has to reach the grid ITEM ---------------------------------

test("a cell child gives its wrapper the span", () => {
  // GridBlock wraps every child to give it min-w-0, and that wrapper is
  // the grid item. A span left on the cell itself styles a block INSIDE
  // a grid cell and does nothing — the cell takes one column and the
  // ones it should have covered sit empty. That shipped: a 3-column bar
  // with a span=2 histogram rendered as three equal columns with the
  // last one blank.
  assert.deepStrictEqual(
    gridItemStyleFor({ type: "cell", props: { span: 2 } }),
    { gridColumn: "span 2 / span 2" },
  );
});

test("an ordinary child gets no span", () => {
  assert.strictEqual(gridItemStyleFor({ type: "markdown", props: {} }), undefined);
  assert.strictEqual(gridItemStyleFor({ type: "column", props: {} }), undefined);
});

test("a cell of span 1 adds no style", () => {
  assert.strictEqual(gridItemStyleFor({ type: "cell", props: { span: 1 } }), undefined);
  assert.strictEqual(gridItemStyleFor({ type: "cell", props: {} }), undefined);
});

test("a malformed child does not throw", () => {
  // Compiled graphs outlive the code that wrote them.
  assert.strictEqual(gridItemStyleFor(null), undefined);
  assert.strictEqual(gridItemStyleFor({}), undefined);
  assert.strictEqual(gridItemStyleFor({ type: "cell" }), undefined);
  assert.strictEqual(gridItemStyleFor({ type: "cell", props: null }), undefined);
});

console.log(`gridStyle: ${passed} tests passed`);
