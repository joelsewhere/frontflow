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


class RedistributionEditor(Operator):
    """A bucket-to-bucket redistribution editor. Renders an editable
    histogram in the frontend (kind id `widget_redistribution` for the
    compiled block; `redistribution_editor` for the rendered widget).

    The user is shown an ordered histogram where two disjoint subsets
    of buckets are designated *sources* (need to be redistributed)
    and *destinations* (available targets). The user picks a policy
    — a redistribution strategy — and either watches the result
    apply deterministically or builds up custom operations via brush
    selection. The submitted value is a bucket-to-bucket mapping
    plus, for manual policy, the operations audit trail.

    `data` is an ordered list of `{"key": str, "count": int}` rows
    or an equivalent dict `{key: count}`. The widget renders bars
    in the order received — caller is responsible for meaningful
    ordering.

    `sources` is a list of bucket keys (all members of `data`) that
    need to be redistributed. `destinations` is a list of bucket
    keys (also members of `data`) that are valid targets. Sources
    and destinations must be disjoint.

    `policies` is the list of policy options the user can pick from
    in the widget. Defaults to ALL available policies if omitted —
    pass an explicit subset to restrict. Valid policies:
      - "spread_even" — distribute source records evenly across all
        destinations
      - "match_shape" — distribute proportionally to destinations'
        existing counts (preserves the in-range shape)
      - "push_to_nearest" — each source bucket goes 100% to its
        nearest destination by position in `data`
      - "manual" — user builds operations via brush selection (the
        most fine-grained option)
      - "drop" — discard all source records

    `default_policy` is the policy initially active when the widget
    opens. Defaults to "manual" — the operation builder is visible
    immediately, making the widget's interactive nature obvious.
    Users can toggle to a deterministic policy (spread_even,
    match_shape, push_to_nearest, drop) at any point and see the
    consequence in the histogram preview. Must be a member of
    `policies` if both are passed.

    Each of `data`, `sources`, `destinations` may be a literal value
    (baked at compile time) or a `StepRef` (resolved at runtime
    against the submission's `steps` namespace).
    """

    kind = "widget_redistribution"

    # The full set of policies the widget knows how to render. The
    # `policies` argument is validated against this; passing
    # anything outside this set is a config error. Adding a new
    # policy means updating both this set AND the widget's
    # frontend strategy dispatch in state.ts.
    _ALL_POLICIES = (
        "spread_even",
        "match_shape",
        "push_to_nearest",
        "manual",
        "drop",
    )

    def __init__(
        self,
        *,
        data,
        sources,
        destinations,
        id: str,
        label: Optional[str] = None,
        required: bool = False,
        policies: Optional[list[str]] = None,
        default_policy: str = "manual",
        value_label: str = "records",
    ) -> None:
        super().__init__(id=id)
        if not isinstance(data, (dict, list, StepRef)):
            raise TypeError(
                f"RedistributionEditor {id!r} got data of type "
                f"{type(data).__name__}; expected a list, dict, or "
                f"StepRef (steps.<node>.<field>)."
            )
        if not isinstance(sources, (list, StepRef)):
            raise TypeError(
                f"RedistributionEditor {id!r} got sources of type "
                f"{type(sources).__name__}; expected a list or "
                f"StepRef."
            )
        if not isinstance(destinations, (list, StepRef)):
            raise TypeError(
                f"RedistributionEditor {id!r} got destinations of "
                f"type {type(destinations).__name__}; expected a "
                f"list or StepRef."
            )

        # Default policies: ALL of them. Form authors restrict by
        # passing an explicit subset.
        if policies is None:
            policies = list(self._ALL_POLICIES)
        valid_policies = set(self._ALL_POLICIES)
        bad = [p for p in policies if p not in valid_policies]
        if bad:
            raise ValueError(
                f"RedistributionEditor {id!r}: unknown policies "
                f"{bad!r}; valid options are "
                f"{sorted(valid_policies)!r}."
            )
        if not policies:
            raise ValueError(
                f"RedistributionEditor {id!r}: `policies` must "
                f"contain at least one option."
            )
        if default_policy not in policies:
            raise ValueError(
                f"RedistributionEditor {id!r}: default_policy "
                f"{default_policy!r} is not in policies "
                f"{policies!r}."
            )

        self.data = data
        self.sources = sources
        self.destinations = destinations
        self.label = (
            label if label is not None
            else id.replace("_", " ").title()
        )
        self.required = required
        self.policies = policies
        self.default_policy = default_policy
        self.value_label = value_label


class Categorizer(Operator):
    """A drag-to-classify board. Renders a bank of item chips and one
    column per category (kind id `widget_categorizer` for the compiled
    block; `categorizer` for the rendered widget); the user drags each
    chip from the bank into a column. An item lives in at most one
    column at a time, so overlapping classifications are structurally
    impossible — no validation needed. Items left in the bank are
    simply unclassified.

    The submitted value maps each category id to the items dropped in
    it:

        {"revenue": ["RENT", "UTIL"], "employee": ["EMP"], ...}

    Every category id appears in the value, empty list when nothing
    was dropped there.

    `options` is the item bank — a list of strings, or a `StepRef`
    resolved at runtime (e.g. `steps.upload.extract_codes`).

    `categories` is an ordered list of `{"id": ..., "label": ...}`
    dicts — one column each. Ids must be unique and non-empty; they
    become the keys of the submitted value. An optional `"color"`
    (any CSS color, e.g. `"#2e7d52"`) tints the column's heading,
    top border, and drop highlight so the columns read as visually
    distinct targets.

    `required` means at least one item must be assigned to some
    category. Per-category minimums are the form's business rules —
    enforce them in a chain `@backend` that raises.

    `bank_label` is the heading over the unassigned bank.
    """

    kind = "widget_categorizer"

    def __init__(
        self,
        *,
        options,
        categories: list,
        id: str,
        label: Optional[str] = None,
        required: bool = False,
        bank_label: str = "Unassigned",
    ) -> None:
        super().__init__(id=id)
        if not isinstance(options, (list, StepRef)):
            raise TypeError(
                f"Categorizer {id!r} got options of type "
                f"{type(options).__name__}; expected a list or a "
                f"StepRef (steps.<node>.<field>)."
            )
        if not isinstance(categories, list) or not categories:
            raise ValueError(
                f"Categorizer {id!r}: `categories` must be a "
                f"non-empty list of {{'id', 'label'}} dicts."
            )
        seen: set[str] = set()
        for cat in categories:
            if not isinstance(cat, dict) or not cat.get("id"):
                raise ValueError(
                    f"Categorizer {id!r}: every category needs a "
                    f"non-empty 'id'; got {cat!r}."
                )
            if cat["id"] in seen:
                raise ValueError(
                    f"Categorizer {id!r}: duplicate category id "
                    f"{cat['id']!r}."
                )
            seen.add(cat["id"])
        self.options = (
            options if isinstance(options, StepRef) else list(options)
        )
        self.categories = [
            {
                "id": c["id"],
                "label": c.get("label") or c["id"],
                **({"color": c["color"]} if c.get("color") else {}),
            }
            for c in categories
        ]
        self.label = (
            label if label is not None
            else id.replace("_", " ").title()
        )
        self.required = required
        self.bank_label = bank_label


__all__ = ["DistributionFilter", "RedistributionEditor", "Categorizer"]
