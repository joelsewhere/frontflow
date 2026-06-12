"""Regression for the routing-failure bug: a `@backend.branch` with
MULTIPLE downstream targets must explicitly name one — returning
None is only valid when the branch has exactly one downstream.

Background: a branch with one downstream can return None to fall
through. Authors reasonably extend that to multi-downstream by
treating one as the "default" and returning None for it, but the
runtime correctly rejects this — with multiple downstreams, None
is ambiguous and the branch must choose. The error message names
the branch step (`need_distribution: routing failed: ...`), which
in a chain UI can be visually attributed to the neighboring node.
"""
from __future__ import annotations

import pytest

from frontflow import Button, backend, displays, form, inputs, node, steps
from frontflow.dsl.compile import compile_workflow
from frontflow.dsl.core import WORKFLOWS
from frontflow.dsl import runtime


@pytest.fixture(autouse=True)
def _isolate():
    yield
    WORKFLOWS.clear()


def _build_workflow(routing_returns_none_on_default: bool):
    """A four-step workflow:
        configure → branch → optional → done
                          ↘___________↗
    The branch picks between `optional` (when there's data to
    handle) and `done` (when there's nothing). Returns either an
    explicit target id or None depending on the parameter — the
    test compares both modes.
    """
    @form(form_id="branch_multi_ds", title="x")
    def f():

        @node
        def landing():
            name = inputs.Text(id="name", label="Name", required=True)
            return name, Button("Next")

        @node
        def configure():
            has_data = inputs.Text(id="has_data", label="x")
            submit = Button("Apply")
            return has_data, submit

        @node
        def optional():
            return displays.Markdown("optional"), Button("Continue")

        @node
        def done():
            return (displays.Markdown("done"),)

        @backend.branch(hidden=True)
        def route(has_data):
            if has_data:
                return None if routing_returns_none_on_default else "optional"
            return "done"

        ln = landing()
        cfg = configure()
        opt = optional()
        dn = done()
        r = route(steps.configure["has_data"])

        ln >> cfg >> r >> opt >> dn
        r >> dn

    f()
    return compile_workflow(WORKFLOWS["branch_multi_ds"])


def test_branch_returning_none_with_multi_downstream_raises():
    """The runtime rejects branch returning None when multiple
    downstreams are wired. The error is recorded on the branch
    step itself, and the submission is marked failed."""
    cw = _build_workflow(routing_returns_none_on_default=True)
    sub = runtime.start_submission(cw, {"name": "Connor"})
    runtime.advance(cw, sub)
    runtime.submit_step(cw, sub, "configure",
                        {"has_data": "yes"}, button_clicked=None)
    runtime.advance(cw, sub)

    assert sub.failed, "expected submission to fail on routing error"
    branch_step = next(s for s in sub.steps if s.node_id == "route")
    assert "multiple downstream steps" in (branch_step.error or "")
    assert "did not choose one" in (branch_step.error or "")


def test_branch_returning_target_id_routes_cleanly():
    """The fix: return the target id explicitly. With both
    downstreams named, routing succeeds and the submission
    progresses through the chosen target."""
    cw = _build_workflow(routing_returns_none_on_default=False)
    sub = runtime.start_submission(cw, {"name": "Connor"})
    runtime.advance(cw, sub)
    runtime.submit_step(cw, sub, "configure",
                        {"has_data": "yes"}, button_clicked=None)
    runtime.advance(cw, sub)

    assert not sub.failed, (
        f"expected clean routing; got errors: "
        + "; ".join(f"{s.node_id}: {s.error}"
                    for s in sub.steps if s.error)
    )
    # The user landed at `optional` (the with-data path).
    assert sub.steps[-1].node_id == "optional"


def test_branch_returning_skip_target_routes_cleanly():
    """The other side of the branch: returning the explicit
    skip target jumps past `optional` straight to `done`."""
    cw = _build_workflow(routing_returns_none_on_default=False)
    sub = runtime.start_submission(cw, {"name": "Connor"})
    runtime.advance(cw, sub)
    runtime.submit_step(cw, sub, "configure",
                        {"has_data": ""}, button_clicked=None)  # empty → skip
    runtime.advance(cw, sub)

    assert not sub.failed
    # Skipped optional; submission terminated at done.
    submitted_node_ids = [s.node_id for s in sub.steps if s.is_submitted]
    assert "optional" not in submitted_node_ids
    assert sub.terminated
