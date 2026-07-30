"""`backend_group` — presentational grouping of workflow-level
@backend steps for the chain UI."""
from __future__ import annotations

import pytest

from frontflow import (
    Button, backend, backend_group, displays, form, inputs, node, steps,
)
from frontflow.dsl.compile import CompiledBackendStep, compile_workflow
from frontflow.dsl.core import WORKFLOWS


@pytest.fixture(autouse=True)
def _isolate():
    yield
    WORKFLOWS.clear()


def test_group_captured_and_compiled():
    @form(form_id="grp")
    def f():
        @node
        def landing():
            return inputs.Text(id="x"), Button("Go")

        @backend
        def a(v):
            return 1

        @backend
        def b(v):
            return 2

        @backend
        def c(v):
            return 3

        @node
        def done():
            return (displays.Markdown("end"),)

        l = landing()
        with backend_group("Building report"):
            sa = a(steps.landing["x"])
            sb = b(steps.landing["x"])
        sc = c(steps.landing["x"])  # outside the group
        d = done()
        l >> sa >> sb >> sc >> d

    f()
    cw = compile_workflow(WORKFLOWS["grp"])
    by_id = {
        s.id: s for s in cw.steps if isinstance(s, CompiledBackendStep)
    }
    assert by_id["a"].group_id == "building_report"
    assert by_id["a"].group_title == "Building report"
    assert by_id["b"].group_id == "building_report"
    # Steps outside the `with` stay ungrouped.
    assert by_id["c"].group_id is None
    assert by_id["c"].group_title is None


def test_group_id_slug_and_override():
    assert backend_group("Building report!").id == "building_report"
    assert backend_group("x", group_id="custom").id == "custom"
    with pytest.raises(ValueError):
        backend_group("!!!")
