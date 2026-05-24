"""Widget operators. Each widget corresponds to a registered widget in
the frontend (see frontend/src/components/widgets/registry.ts).

A widget is constructed directly, like an input:

    date_range = widgets.DistributionFilter(
        id="date_range",
        label="Date range",
        value_label="records",
        data={"2025-01-06": 3, "2025-01-13": 5, ...},
    )

`data` may be a literal dict (compile-time baked) or a `StepRef`
resolved at runtime — e.g. `steps.<backend_name>.return` to source
the data from a `@backend`'s output:

    @backend
    def fetch_counts(steps):
        from frontflow.aws.hooks import S3Hook
        return S3Hook().read_json(
            bucket="b", key=f"runs/{steps.start.run_id}/counts.json",
        )

    date_range = widgets.DistributionFilter(
        id="date_range",
        data=steps.fetch_counts.return,
    )

The author needs to name the widget — pass `id` explicitly.
"""

from __future__ import annotations

from typing import Any, Optional

from .core import Operator
from .references import StepRef


class DistributionFilter(Operator):
    """A filterable histogram. Renders the histogram-with-selection
    widget in the frontend (kind id `widget_histogram` for the
    compiled block; `distribution_filter` for the rendered widget).
    The user drags a range; the submitted value is `{start, end}` —
    the x-axis labels at the selection bounds.

    `data` is a `{x_value: count}` mapping. X-axis keys may be ISO
    date strings or numbers (ints/floats) — the widget infers the axis
    type from the keys; it is a generic range filter, not a date-only
    one. Pass either a literal dict (baked at compile time) or a
    `StepRef` (resolved at runtime against the submission's `steps`
    namespace).

    `value_label` is the human-readable noun used in the widget's
    tooltips ("3 records", "10 events"); defaults to "value".
    """

    kind = "widget_histogram"

    def __init__(
        self,
        *,
        data,
        id: str,
        label: Optional[str] = None,
        required: bool = False,
        value_label: str = "value",
    ) -> None:
        super().__init__(id=id)
        if not isinstance(data, (dict, StepRef)):
            raise TypeError(
                f"DistributionFilter {id!r} got data of type "
                f"{type(data).__name__}; expected a dict or a StepRef "
                f"(steps.<node>.<field>)."
            )
        self.data = data
        self.label = (
            label if label is not None
            else id.replace("_", " ").title()
        )
        self.required = required
        self.value_label = value_label


__all__ = ["DistributionFilter"]
