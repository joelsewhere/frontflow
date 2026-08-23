"""Tests for `superset.Filter` — filters declared in the DSL.

The claim: a filter an author names is CREATED in Superset, so
declaring one and driving it with `SetFilters` are the same word in the
same file with nothing to click in between.

The generated SQL matters as much as the filter. Every form writes into
one `form_values` column, so a key means whatever the form that wrote it
meant — one form storing `units` as a number and another as an object is
normal. An unguarded cast then fails the whole chart with `invalid input
syntax for type numeric`, which is a real failure this project hit.
`TestGeneratedSQL` is there so it cannot come back.
"""
from __future__ import annotations

import pytest

from frontflow import displays, superset
from frontflow.superset.filters import KINDS, expression_for


class TestDeclaration:
    def test_a_filter_needs_a_name(self):
        with pytest.raises(ValueError):
            superset.Filter("")

    def test_it_needs_exactly_one_of_field_or_column(self):
        """A field is extracted out of form_values; a column already
        exists. Both, or neither, is an author who has not decided."""
        with pytest.raises(ValueError, match="exactly one"):
            superset.Filter("Region")
        with pytest.raises(ValueError, match="exactly one"):
            superset.Filter("Region", field="region", column="region")

    def test_the_kind_must_be_known(self):
        with pytest.raises(ValueError, match="kind must be one of"):
            superset.Filter("Region", field="region", kind="dropdown")

    def test_a_field_becomes_the_column_name(self):
        assert superset.Filter("Region", field="region").column == "region"

    def test_a_dashboard_rejects_anything_else_in_filters(self):
        with pytest.raises(TypeError, match="superset.Filter"):
            displays.Dashboard("d", filters=["Region"])


class TestGeneratedSQL:
    """What gets written into Superset as a calculated column."""

    def test_a_value_filter_reads_only_strings(self):
        sql = expression_for("region", "value")
        assert "jsonb_typeof(form_values->'region') = 'string'" in sql
        assert "form_values->>'region'" in sql

    def test_a_range_filter_casts_only_numbers(self):
        """The regression that motivated this: an unguarded
        `(form_values->>'units')::numeric` fails the entire chart the
        moment any form stores something else under `units`."""
        sql = expression_for("units", "range")
        assert "jsonb_typeof(form_values->'units') = 'number'" in sql
        assert "::numeric" in sql

    def test_every_kind_is_guarded(self):
        """No kind may generate a bare cast — including any added
        later."""
        for kind in KINDS:
            sql = expression_for("f", kind)
            assert sql.startswith("CASE WHEN jsonb_typeof("), (kind, sql)
            assert sql.endswith("END"), (kind, sql)

    def test_a_kind_maps_to_the_right_superset_filter(self):
        assert KINDS["value"]["filter_type"] == "filter_select"
        assert KINDS["range"]["filter_type"] == "filter_range"
        assert KINDS["time"]["filter_type"] == "filter_time"


class TestCompilation:
    def test_declared_filters_reach_the_server(self):
        """They ride the compiled block, which is what the embed route
        reads when it provisions."""
        block = displays.Dashboard(
            "sales_overview",
            filters=[
                superset.Filter("Region", field="region"),
                superset.Filter("Units", field="units", kind="range"),
            ],
        )
        assert [f.serialize() for f in block.filters] == [
            {
                "name": "Region",
                "field": "region",
                "column": "region",
                "kind": "value",
            },
            {
                "name": "Units",
                "field": "units",
                "column": "units",
                "kind": "range",
            },
        ]

    def test_a_dashboard_without_filters_declares_none(self):
        """Opt-in: every dashboard written before this must be
        unaffected."""
        assert displays.Dashboard("d").filters == []
