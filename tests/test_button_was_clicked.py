"""Regression for `Button.was_clicked()` — the primitive that lets a
form gate after-submit content (status callouts, confirmation
banners) inline on the submitted page, without splitting into
separate downstream nodes.
"""
from __future__ import annotations

import pytest

from frontflow import (
    Button, backend, displays, form, inputs, node, page,
)
from frontflow.dsl.compile import compile_workflow
from frontflow.dsl.conditions import FieldCondition, When
from frontflow.dsl.core import WORKFLOWS


@pytest.fixture(autouse=True)
def _isolate():
    yield
    WORKFLOWS.clear()


class TestButtonWasClicked:
    def test_returns_field_condition_carrying_button(self):
        """`was_clicked()` produces a FieldCondition whose `field` is
        the Button itself (its id is resolved at compile time via
        the standard same-node mechanism)."""
        btn = Button("Submit", id="go")
        cond = btn.was_clicked()
        assert isinstance(cond, FieldCondition)
        assert cond.field is btn
        assert cond.op == "button_clicked"
        assert cond.value is None
        assert cond.node is None  # same-node condition

    def test_serializes_with_button_id(self):
        """When serialized for the wire, the condition's `field` is
        the button's id — exactly the lookup the frontend uses to
        decide whether THIS button was the one clicked."""
        btn = Button("Submit", id="approve")
        cond = btn.was_clicked()
        wire = cond.serialize()
        assert wire == {
            "field": "approve",
            "op": "button_clicked",
            "value": None,
        }

    def test_compiles_inside_when_on_a_page(self):
        """End-to-end: a flat `@page` with a `Button.was_clicked()`
        gated `displays.When` compiles cleanly. This is the exact
        pattern the property-acquisition form uses for inline
        approve / deny status callouts."""
        @form(form_id="page_was_clicked", title="x")
        def fm():
            @node
            def landing():
                return (
                    inputs.Text(id="x", label="x"),
                    Button("Continue"),
                )

            @page
            def review():
                approve = Button("Approve", id="approve")
                deny = Button("Deny", id="deny", variant="danger")
                return (
                    displays.Markdown("# Report content"),
                    (approve, deny),
                    displays.When(
                        approve.was_clicked(),
                        displays.Markdown("approved"),
                    ),
                    displays.When(
                        deny.was_clicked(),
                        displays.Markdown("rejected"),
                    ),
                )

            ln = landing()
            rv = review()
            ln >> rv  # review is terminal — no fan-out, no branch

        fm()
        cw = compile_workflow(WORKFLOWS["page_was_clicked"])
        assert len(cw.steps) == 2
        review_step = cw.by_id["review"]
        # Terminal — no downstream needed; the inline When blocks
        # surface the result without a separate page transition.
        assert review_step.downstream == []
