"""displays.Grid — a fixed-column layout for filter bars.

`Row` is a flex row: children never wrap and a Button is as wide as the
Select beside it. Grid is the block for a horizontal filter bar, where
the author wants a column count and a Button that lines up with the
bottom of the inputs.

The validation here is front-loaded on purpose. A bad column count that
reaches the browser degrades to a one-column stack, which looks like a
layout that merely did not work rather than a value that was wrong.
"""

from __future__ import annotations

import pytest

from frontflow.dsl import displays
from frontflow.dsl.compile import _compile_block_inner


def _compile(op):
    return _compile_block_inner(op, {"inputs": [], "buttons": []}, "n", [])


def _text(label="x"):
    return displays.Markdown(label)


class TestGridDeclaration:
    def test_columns_and_align_reach_the_compiled_block(self):
        blk = _compile(displays.Grid(_text(), columns=4, align="end"))
        assert blk.type == "grid"
        assert blk.props["columns"] == 4
        assert blk.props["align"] == "end"

    def test_defaults_are_two_columns_stretched(self):
        blk = _compile(displays.Grid(_text()))
        assert blk.props["columns"] == 2
        assert blk.props["align"] == "stretch"

    @pytest.mark.parametrize("columns", [0, -1, 13, 100])
    def test_a_column_count_out_of_range_is_refused(self, columns):
        with pytest.raises(ValueError, match="out of range"):
            displays.Grid(_text(), columns=columns)

    @pytest.mark.parametrize("columns", ["4", 2.5, None, True])
    def test_a_non_integer_column_count_is_refused(self, columns):
        """`True` is an int in Python and would otherwise mean one
        column, which is not what anyone typing it meant."""
        with pytest.raises(ValueError, match="whole number"):
            displays.Grid(_text(), columns=columns)

    def test_an_unknown_alignment_is_refused(self):
        with pytest.raises(ValueError, match="not one of"):
            displays.Grid(_text(), align="sideways")

    @pytest.mark.parametrize("align", list(displays.GRID_ALIGN))
    def test_every_documented_alignment_is_accepted(self, align):
        assert _compile(displays.Grid(_text(), align=align)).props["align"] == align


class TestCell:
    def test_a_span_reaches_the_compiled_block(self):
        blk = _compile(displays.Cell(_text(), span=2))
        assert blk.type == "cell"
        assert blk.props["span"] == 2

    def test_the_default_span_is_one(self):
        assert _compile(displays.Cell(_text())).props["span"] == 1

    @pytest.mark.parametrize("span", [0, -1, 13])
    def test_a_span_out_of_range_is_refused(self, span):
        with pytest.raises(ValueError, match="out of range"):
            displays.Cell(_text(), span=span)

    def test_a_non_integer_span_is_refused(self):
        with pytest.raises(ValueError, match="whole number"):
            displays.Cell(_text(), span="2")

    def test_cells_nest_inside_a_grid(self):
        blk = _compile(
            displays.Grid(
                displays.Cell(_text("wide"), span=2),
                _text("narrow"),
                columns=3,
            )
        )
        assert [c.type for c in blk.children] == ["cell", "markdown"]
        assert blk.children[0].props["span"] == 2


class TestGridCarriesInputs:
    def test_inputs_inside_a_grid_are_still_collected(self):
        """A Grid is a container like any other — a field inside one has
        to reach the node's input list, or the form would render it and
        then ignore what was typed."""
        from frontflow.dsl import inputs

        collected = {"inputs": [], "buttons": []}
        _compile_block_inner(
            displays.Grid(inputs.Text(id="region"), columns=2),
            collected,
            "n",
            [],
        )
        assert [op.id for op, _ in collected["inputs"]] == ["region"]

    def test_inputs_inside_a_cell_are_still_collected(self):
        from frontflow.dsl import inputs

        collected = {"inputs": [], "buttons": []}
        _compile_block_inner(
            displays.Grid(
                displays.Cell(inputs.Text(id="notes"), span=2), columns=2
            ),
            collected,
            "n",
            [],
        )
        assert [op.id for op, _ in collected["inputs"]] == ["notes"]
