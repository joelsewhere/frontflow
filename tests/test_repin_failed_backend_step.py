"""Regression for the failed-submission force-repin bug.

A submission with a failed workflow-level @backend step (e.g. a
routing failure on a @backend.branch) was being truncated all the
way back to the landing node when force-repinned. Two root causes:

  1. `validate_repin` looked up steps via `live.all_nodes_by_id`,
     which contains nodes only — not workflow-level backend steps.
     Every submitted backend step was flagged as `node_missing`,
     tainting the chain at the earliest one.

  2. `selective_force_repin_submission`'s resume logic also used
     `all_nodes_by_id`, so even if a backend step was correctly
     identified as the truncate point, the resume target couldn't
     be materialized — it fell back to the landing node, losing
     all prior work.

The fix: look up via `live.by_id` (which contains both nodes and
backend steps). A failed backend step is implicitly tainted (with
a `failed_backend_step` issue), so the truncate logic correctly
drops it and re-materializes it on the new pin; `advance()` then
re-runs it on the fixed form. Successful backend steps are kept
as-is (their `backend_return` is the canonical output).
"""
from __future__ import annotations

import pytest

from frontflow import Button, backend, displays, form, inputs, node, steps
from frontflow.dsl.compile import compile_workflow
from frontflow.dsl.core import WORKFLOWS
from frontflow.dsl import runtime
from frontflow.dsl.runtime import (
    Submission,
    selective_force_repin_submission,
    validate_repin,
)


@pytest.fixture(autouse=True)
def _isolate():
    yield
    WORKFLOWS.clear()


def _build_form(*, route_target: str, form_id: str = "repin_failed"):
    """Builds a workflow with a branch step. `route_target` is
    what the branch returns. Pass `"missing"` (a non-existent id)
    to make the branch fail with a routing error — that's the
    state Connor's submission was in.

    `form_id` lets a test build two variants (old + new) without
    colliding on the workflow registry."""

    @form(form_id=form_id, title="x")
    def f():

        @node
        def landing():
            name = inputs.Text(id="name", label="Name", required=True)
            return name, Button("Next")

        @node
        def configure():
            x = inputs.Text(id="x", label="x")
            return x, Button("Apply")

        @node
        def left():
            return displays.Markdown("left"), Button("Done")

        @node
        def right():
            return displays.Markdown("right"), Button("Done")

        @backend.branch(hidden=True)
        def route(x):
            return route_target

        ln = landing()
        cfg = configure()
        lf = left()
        rt = right()
        r = route(steps.configure["x"])

        ln >> cfg >> r >> lf
        r >> rt

    f()
    return compile_workflow(WORKFLOWS[form_id])


def _drive_to_failed_route(cw):
    """Returns a submission with a failed `route` backend step."""
    sub = runtime.start_submission(cw, {"name": "Connor"})
    runtime.advance(cw, sub)
    runtime.submit_step(cw, sub, "configure",
                        {"x": "anything"}, button_clicked=None)
    runtime.advance(cw, sub)
    assert sub.failed, (
        "test fixture should produce a failed submission; "
        f"steps: {[(s.node_id, s.is_submitted, s.error) for s in sub.steps]}"
    )
    return sub


class TestValidateRepinHandlesBackendSteps:
    """Successful backend steps don't generate spurious issues;
    failed ones are tainted with the `failed_backend_step` kind."""

    def test_failed_backend_step_is_tainted(self):
        cw_bad = _build_form(route_target="does_not_exist", form_id="repin_bad")
        sub = _drive_to_failed_route(cw_bad)
        cw_fixed = _build_form(route_target="right", form_id="repin_fixed")

        issues = validate_repin(cw_bad, cw_fixed, sub)
        kinds = [i["kind"] for i in issues]
        assert "failed_backend_step" in kinds, (
            f"expected failed_backend_step taint; got {issues}"
        )
        # Crucially NOT node_missing: the step still exists in
        # the live form.
        assert "node_missing" not in kinds

    def test_successful_backend_step_passes_clean(self):
        """A submission that flowed cleanly past a backend step
        produces no issues on repin against an unchanged form."""
        cw_ok = _build_form(route_target="right", form_id="repin_ok")
        sub = runtime.start_submission(cw_ok, {"name": "Connor"})
        runtime.advance(cw_ok, sub)
        runtime.submit_step(cw_ok, sub, "configure",
                            {"x": "anything"}, button_clicked=None)
        runtime.advance(cw_ok, sub)
        # The branch successfully routed to `right`.
        assert not sub.failed
        # Validate repin against the same workflow (no changes).
        issues = validate_repin(cw_ok, cw_ok, sub)
        assert issues == [], (
            f"unchanged workflow should produce no issues; got {issues}"
        )


class TestForceRepinResumesAtBackendStep:
    """When the truncate point is a backend step, the resume
    target should be that step (re-materialized on the new pin),
    NOT the landing node."""

    def test_force_repin_resumes_at_failed_branch(self):
        cw_bad = _build_form(route_target="does_not_exist", form_id="repin_bad")
        sub = _drive_to_failed_route(cw_bad)
        cw_fixed = _build_form(route_target="right", form_id="repin_fixed")

        summary = selective_force_repin_submission(
            cw_bad, cw_fixed, sub, new_version_id=2,
        )

        # The branch step was dropped (it failed); everything
        # upstream of it was kept (the user shouldn't have to
        # re-do property_name / configure).
        assert "route" in summary["dropped"]
        assert "landing" in summary["kept"]
        assert "configure" in summary["kept"]

        # Resume target is the branch step itself, ready to
        # re-run on the new pin. NOT the landing node.
        assert sub.steps[-1].node_id == "route"
        assert not sub.steps[-1].is_submitted

    def test_force_repin_then_advance_recovers(self):
        """End-to-end recovery: the branch now returns a valid
        target on the new pin. After force-repin + advance, the
        submission progresses past the route to its downstream."""
        cw_bad = _build_form(route_target="does_not_exist", form_id="repin_bad")
        sub = _drive_to_failed_route(cw_bad)
        cw_fixed = _build_form(route_target="right", form_id="repin_fixed")

        selective_force_repin_submission(
            cw_bad, cw_fixed, sub, new_version_id=2,
        )
        # Selective force-repin already resets failed/terminated
        # so the chain is "in flight" again on the new pin.
        assert not sub.failed

        # Advance: re-runs the route step against the new pin's
        # (fixed) branch function. The submission progresses.
        runtime.advance(cw_fixed, sub)

        assert not sub.failed, (
            "submission still failed after repin + advance; "
            f"errors: {[(s.node_id, s.error) for s in sub.steps if s.error]}"
        )
        # The user landed at `right` (the branch's new target).
        submitted_ids = [s.node_id for s in sub.steps if s.is_submitted]
        assert "route" in submitted_ids, (
            "expected route to have run successfully on advance"
        )
        assert sub.steps[-1].node_id == "right"
