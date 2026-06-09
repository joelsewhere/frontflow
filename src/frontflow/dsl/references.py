"""
Step references — `steps.<node>.<field>` reads a value from another
node into the place a static value would go.

This is the same `steps.<node>.<field>` namespace used everywhere else:
`{{ steps.X.Y }}` templating and the magic `steps` parameter of
`@displays.branch`. One namespace, regardless of where you reach for a
value.

`steps.<node>.<field>` is a lazy, symbolic reference — it names a value
without resolving it. `<field>` is an input id or a `@backend` function
name in that node. Pass it where a static value would go:

    primary_region = inputs.Select(
        label="Primary region",
        options=steps.basics.regions,    # choices = a prior step's value
    )

    confirm_name = inputs.Text(
        label="Confirm your name",
        default=steps.intake.full_name,  # pre-filled from an earlier node
    )

The reference is inert at definition time — `steps.basics.regions` just
records the names, so a node may reference one defined later in the
file. It is resolved server-side each time the node is served for a
submission, against that submission's accumulated step data.

When used this way — for `options`, `default`, or a `When` condition —
the referenced node must be an *earlier* node: the value is read from
submitted data, and the current node hasn't been submitted yet. (In a
`{{ steps.X.Y }}` *template* you may also name the current node, which
resolves live in the browser — see runtime.py.)

A node id that begins with `_` (unusual) must use the item form:
`steps["_odd"]["field"]`.
"""

from __future__ import annotations

import re

# The canonical template token: `{{ steps.<node>.<field> }}` — the one
# namespace shared by labels, button urls, submission_id and run_id.
# Used by the compiler (dependency collection) and the runtime
# (resolution), so it lives here, beside the `steps` accessor.
#
# A template token is `steps.<node>.<name>` (form value, standalone
# backend's return, or an inner namespace) and may optionally drill
# one level deeper — `steps.<node>.<step_id>.<field>` — to read a
# chain-step output (operator state field, backend return key) by name.
STEP_REF_RE = re.compile(
    r"\{\{\s*steps\s*\.\s*([A-Za-z_]\w*)"
    r"\s*\.\s*([A-Za-z_]\w*)"
    r"(?:\s*\.\s*([A-Za-z_]\w*))?"
    # Optional Jinja filter chain — `| lower`, `| upper | trim`,
    # `| default("none") | upper`, etc. Anything up to the closing
    # `}}` that isn't itself `}}`. The captured triple is unchanged;
    # the filter chain is just a non-capturing tail so the compile-
    # time dependency scanner still finds (node, field) references
    # in templates that use filters.
    r"(?:\s*\|[^}]*?)?"
    r"\s*\}\}"
)

# Block props whose string value may carry `{{ steps.X.Y }}` templates.
# The compiler scans these for dependencies; the runtime resolves them.
# Block props that carry `{{ steps.X.Y }}` templates — resolved at
# runtime and scanned for dependencies at compile time. `label` and
# `url` are interactive-element props; `source` is a Markdown block's
# prose; `key` is an S3 object key on a download block; `value` is a
# KPI block's metric.
TEMPLATED_PROPS = ("label", "url", "source", "key", "value")


class StepRef:
    """A symbolic pointer into node `<node_id>` — inert until the
    runtime resolves it against a submission's data.

    `name` selects what is read:
      - a field id / `@backend` function name — a *field reference*,
        `steps.<node>.<field>`, resolving to that one value;
      - `None` — a *whole-node reference*, `steps.<node>`, resolving to
        the node's entire value dict. A whole-node reference is the
        author's deliberate "depends on everything in this node": any
        change anywhere in it counts.

    Used as a field's `options` / `default`, a `When` condition, a
    `{{ template }}`, or an argument to a workflow-level `@backend` /
    `@displays.branch`. The scalar/list positions (options, default,
    condition operand, template) require a field reference — a
    whole-node dict has no meaning there; the compiler enforces this.
    """

    def __init__(self, node_id: str, name: object = None) -> None:
        self.node_id = node_id
        # None marks a whole-node reference.
        self.name: object = name

    @property
    def is_whole_node(self) -> bool:
        """True for `steps.<node>` — depends on the entire node."""
        return self.name is None

    def serialize(self) -> dict[str, object]:
        """Compile-time form — shipped in a block's props as an
        `options_from` / `default_from` descriptor, or recorded as a
        backend/branch argument. `name` is None for a whole-node ref."""
        return {"node": self.node_id, "name": self.name}

    # --- Cross-node condition builders ---------------------------------
    # Mirror the same-node builders on the Input base; each yields a
    # FieldCondition carrying this reference's node, for `displays.When`.

    def _condition(self, op: str, value: object = None):
        # Deferred import — conditions.py imports this module.
        from .conditions import FieldCondition

        if self.is_whole_node:
            raise TypeError(
                f"steps.{self.node_id} is a whole-node reference — it has "
                f"no single value to compare. Name a field "
                f"(steps.{self.node_id}.<field>) to build a condition."
            )
        return FieldCondition(self.name, op, value, node=self.node_id)

    def equals(self, value: object):
        """A condition: this field's value equals `value`."""
        return self._condition("equals", value)

    def not_equals(self, value: object):
        """A condition: this field's value differs from `value`."""
        return self._condition("not_equals", value)

    def in_(self, values: object):
        """A condition: this field's value is one of `values`."""
        return self._condition("in", list(values))  # type: ignore[arg-type]

    def not_in(self, values: object):
        """A condition: this field's value is none of `values`."""
        return self._condition("not_in", list(values))  # type: ignore[arg-type]

    def contains(self, value: object):
        """A condition: this field — a list — includes `value`.

        For list-valued fields such as a multi-select, a checkbox list,
        or a HITL operator's `chosen_options`. Works for a single-select
        HITL choice too, since that is a one-element list."""
        return self._condition("contains", value)

    def is_filled(self):
        """A condition: this field has a non-empty value."""
        return self._condition("truthy", None)

    def is_blank(self):
        """A condition: this field is empty."""
        return self._condition("falsy", None)

    def __getattr__(self, name: str) -> "StepRef":
        """`steps.<node>.<field>` — narrow a whole-node reference to a
        field reference. Only valid on a whole-node ref."""
        if name.startswith("_"):
            raise AttributeError(name)
        if not self.is_whole_node:
            raise AttributeError(
                f"steps.{self.node_id}.{self.name} is already a field "
                f"reference — {name!r} can't be read from it."
            )
        return StepRef(self.node_id, name)

    def __getitem__(self, name: str) -> "StepRef":
        """`steps.<node>['field']` — field reference for an id that
        isn't a valid attribute name."""
        if not self.is_whole_node:
            raise TypeError(
                f"steps.{self.node_id}.{self.name} is already a field "
                f"reference."
            )
        return StepRef(self.node_id, name)

    def __repr__(self) -> str:
        if self.is_whole_node:
            return f"<steps {self.node_id} (whole node)>"
        return f"<steps {self.node_id}.{self.name}>"


class _Steps:
    """The `steps` accessor root. `steps.<node>` is a whole-node
    reference (a StepRef); a further `.<field>` narrows it to a field
    reference."""

    def __getattr__(self, node_id: str) -> StepRef:
        if node_id.startswith("_"):
            raise AttributeError(node_id)
        return StepRef(node_id)

    def __getitem__(self, node_id: str) -> StepRef:
        return StepRef(node_id)


# The singleton authors import: `from workflows import steps`.
steps = _Steps()
