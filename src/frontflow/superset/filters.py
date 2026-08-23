"""Native filters a dashboard should have, declared in the DSL.

    displays.Dashboard(
        "sales_overview",
        filters=[
            superset.Filter("Region", field="region"),
            superset.Filter("Units", field="units", kind="range"),
        ],
    )

frontflow already provisions a dashboard and its refresh filter on first
use. This extends that to the filters an author actually drives: the
name here is the name `superset.SetFilters` refers to, so declaring one
and driving it are the same word in both places.

**Why a field needs a column.** Form answers live in a single JSONB
`form_values`, so there is nothing for a filter to point at until one is
extracted. The provisioner adds a calculated column for the field and
targets the filter at it.

**Why the extraction is guarded.** Every form writes into that one
column, so a key means whatever the form that wrote it meant — one form
storing `units` as a number and another as an object is normal, not a
mistake. An unguarded `(form_values->>'units')::numeric` then fails the
whole chart with `invalid input syntax for type numeric`. The generated
expression casts only what `jsonb_typeof` confirms, and yields NULL
otherwise.
"""

from __future__ import annotations

from typing import Any, Optional

# How a declared filter maps onto Superset and onto SQL.
#
#   value — pick one or several; drives an IN clause
#   range — two numeric bounds; drives >= / <=
#   time  — a time range, the shape RefreshDashboard drives
KINDS: dict[str, dict[str, str]] = {
    "value": {
        "filter_type": "filter_select",
        "json_type": "string",
        "cast": "{extract}",
        "column_type": "STRING",
    },
    "range": {
        "filter_type": "filter_range",
        "json_type": "number",
        "cast": "({extract})::numeric",
        "column_type": "NUMERIC",
    },
    "time": {
        "filter_type": "filter_time",
        "json_type": "string",
        "cast": "({extract})::timestamptz",
        "column_type": "TIMESTAMP",
    },
}


class Filter:
    """One native filter a dashboard should have.

    `name` is what the filter is called on the dashboard's filter bar —
    and what `superset.SetFilters` names to drive it.

    Give either `field` (a form field inside `form_values`, extracted
    into a calculated column) or `column` (a real column that already
    exists on the dataset).
    """

    def __init__(
        self,
        name: str,
        *,
        field: Optional[str] = None,
        column: Optional[str] = None,
        kind: str = "value",
    ) -> None:
        if not name or not str(name).strip():
            raise ValueError(
                'superset.Filter needs a name, e.g. superset.Filter("Region", '
                'field="region")'
            )
        if kind not in KINDS:
            raise ValueError(
                f"superset.Filter {name!r}: kind must be one of "
                f"{tuple(KINDS)}; got {kind!r}."
            )
        if bool(field) == bool(column):
            raise ValueError(
                f"superset.Filter {name!r} needs exactly one of `field` (a "
                f"form field, extracted from form_values into a calculated "
                f"column) or `column` (a column the dataset already has)."
            )

        self.name = str(name).strip()
        self.field = field
        self.column = column or field
        self.kind = kind

    def serialize(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "field": self.field,
            "column": self.column,
            "kind": self.kind,
        }

    def __repr__(self) -> str:
        return f"<superset.Filter {self.name!r} kind={self.kind!r}>"


def expression_for(field: str, kind: str) -> str:
    """SQL extracting `field` from `form_values`, for a filter of `kind`.

    Guarded on `jsonb_typeof`: a key means whatever the form that wrote
    it meant, and an unguarded cast takes the whole chart down when some
    other form stored something else under the same name.
    """
    spec = KINDS[kind]
    extract = f"form_values->>'{field}'"
    cast = spec["cast"].format(extract=extract)
    return (
        f"CASE WHEN jsonb_typeof(form_values->'{field}') = "
        f"'{spec['json_type']}' THEN {cast} END"
    )
