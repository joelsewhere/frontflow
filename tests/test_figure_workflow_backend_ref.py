"""Tests for `_validate_figure_data_refs`.

The Figure block's `data` argument can reference bytes from two
sources:

  - a node-internal @backend whose return is bytes — reference
    shape `steps.<node>.<backend_fn>`;
  - a workflow-level @backend step whose return is bytes —
    reference shape `steps.<step_id>` (whole-node-shaped, since
    a standalone backend step's namespace IS its return value).

The inline `_compile_block` Figure path can't distinguish these at
compile time (both look structurally the same), so the validation
pass `_validate_figure_data_refs` runs in `compile_workflow` after
all steps are compiled and rejects whole-node refs pointing at
nodes — the common author-error case `steps.<node>` instead of
`steps.<node>.<backend>`.

These tests cover:
  - whole-node ref to a workflow-level backend → OK
  - whole-node ref to a node → rejected (helpful message)
  - field ref to a node-internal backend → OK (existing behavior)
"""
from __future__ import annotations

import pytest

from frontflow import (
    Button, backend, displays, form, node, steps,
)
from frontflow.dsl.compile import compile_workflow
from frontflow.dsl.core import WORKFLOWS


@pytest.fixture(autouse=True)
def _isolate_workflows():
    """Each test gets its own clean workflow registry."""
    yield
    WORKFLOWS.clear()


class TestWorkflowBackendFigureRef:
    """A Figure pointing at a workflow-level @backend step via a
    whole-node ref `steps.<step_id>` compiles cleanly. The runtime
    resolves the ref to the backend's return (a blob handle dict)
    just as it would for a node-internal @backend."""

    def test_workflow_backend_ref_compiles(self):
        @form(form_id="ok_wf_backend_figure", title="OK")
        def f():
            @node
            def landing():
                return (
                    displays.Markdown("hi"),
                    Button("Go"),
                )

            @backend(hidden=True)
            def make_image():
                return b"PNG-bytes-here"

            @node
            def show():
                return displays.Figure(
                    data=steps.make_image,
                    caption="A chart",
                    alt="Chart",
                )

            ln = landing()
            sh = show()
            mi = make_image()
            ln >> mi >> sh

        f()
        cw = compile_workflow(WORKFLOWS["ok_wf_backend_figure"])
        # The figure block ends up inside the `show` node's layout —
        # find it and confirm the data_from descriptor points at the
        # workflow backend with `name: None`.
        show_node = cw.all_nodes_by_id["show"]

        def find_figure(block):
            if block.type == "figure":
                return block
            for c in block.children:
                hit = find_figure(c)
                if hit is not None:
                    return hit
            return None

        fig = find_figure(show_node.layout)
        assert fig is not None
        assert fig.props["data_from"] == {
            "node": "make_image", "name": None,
        }


class TestNodeFieldFigureRef:
    """A Figure pointing at a node-internal @backend via a field
    ref `steps.<node>.<backend_fn>` is the original supported shape
    — it must still compile cleanly with no validation noise."""

    def test_node_internal_backend_ref_compiles(self):
        @form(form_id="ok_node_internal_figure", title="OK")
        def f():
            @node
            def landing():
                return (
                    displays.Markdown("hi"),
                    Button("Go"),
                )

            @node
            def show():
                submit = Button("Done")

                @backend
                def make_image(steps):
                    return b"PNG-bytes-here"

                submit >> make_image()

                return (
                    submit,
                    displays.Figure(
                        data=steps.show.make_image,
                        caption="A chart",
                        alt="Chart",
                    ),
                )

            landing() >> show()

        f()
        cw = compile_workflow(WORKFLOWS["ok_node_internal_figure"])
        show_node = cw.all_nodes_by_id["show"]

        def find_figure(block):
            if block.type == "figure":
                return block
            for c in block.children:
                hit = find_figure(c)
                if hit is not None:
                    return hit
            return None

        fig = find_figure(show_node.layout)
        assert fig is not None
        assert fig.props["data_from"] == {
            "node": "show", "name": "make_image",
        }


class TestWholeNodeRefToNodeRejected:
    """A Figure with `data=steps.<some_node>` (whole-node ref to a
    node) is almost certainly an author error — `steps.<node>` is
    a dict of submitted form values, not bytes. The validation pass
    rejects it with a message naming the likely fix."""

    def test_whole_node_ref_to_node_raises(self):
        with pytest.raises(ValueError, match="Figure data uses a "
                           "whole-node reference"):
            @form(form_id="bad_whole_node_figure", title="Bad")
            def f():
                @node
                def landing():
                    return (
                        displays.Markdown("hi"),
                        Button("Go"),
                    )

                @node
                def show():
                    return (
                        displays.Figure(
                            data=steps.landing,  # ← invalid: a node
                            caption="Bad",
                            alt="Bad",
                        ),
                        Button("Done"),
                    )

                landing() >> show()

            f()
            compile_workflow(WORKFLOWS["bad_whole_node_figure"])

    def test_rejection_names_the_referenced_target(self):
        # The error message includes the target node id so the
        # author can find the offending line quickly.
        with pytest.raises(ValueError, match=r"`steps\.landing`"):
            @form(form_id="bad_named_target", title="Bad")
            def f():
                @node
                def landing():
                    return (
                        displays.Markdown("hi"),
                        Button("Go"),
                    )

                @node
                def show():
                    return (
                        displays.Figure(data=steps.landing, alt="x"),
                        Button("Done"),
                    )

                landing() >> show()

            f()
            compile_workflow(WORKFLOWS["bad_named_target"])
