"""Regression for flat-`@page` bodies hosting chain backends.

Two framework gaps were blocking this — Connor wanted to make a form's
review screen a `@page` (for the larger visual real estate that a page
offers over a `@node`) while keeping its `@backend.branch` chained to
approve / deny buttons:

  1. `PageTemplate.__call__` only set `_building_page` during the body,
     not `_building_node`. So `@backend.__call__` saw "no node context"
     and treated the inner `decision(...)` call as a workflow-scope
     step, rejecting the Button arguments with a TypeError demanding
     `steps.<x>` references.

  2. `_step_is_branch` only handled `CompiledBackendStep` and
     `CompiledNode`. A flat `CompiledPage` whose implicit node hosts
     an inner `@backend.branch` would still report `is_branch=False`,
     so the edge validator rejected the workflow-level fan-out to
     approved/rejected with "not a @backend.branch".

Both are fixed: flat pages now behave identically to @node bodies for
chain wiring, AND a flat-page step's branch nature is recognised by
the edge validator.
"""
from __future__ import annotations

import pytest

from frontflow import (
    Button, backend, displays, form, inputs, node, page,
)
from frontflow.dsl.compile import (
    compile_workflow, _step_is_branch, CompiledPage,
)
from frontflow.dsl.core import WORKFLOWS


@pytest.fixture(autouse=True)
def _isolate():
    yield
    WORKFLOWS.clear()


def _build_form_with_page_branch(form_id: str):
    """A form whose terminal `review` is a flat `@page` containing an
    inner `@backend.branch` that fans out to approved / rejected."""
    @form(form_id=form_id, title="x")
    def fm():
        @node
        def landing():
            return (
                inputs.Text(id="name", label="Name", required=True),
                Button("Continue"),
            )

        @page
        def review():
            approve = Button("Approve", id="approve")
            deny = Button("Deny", id="deny", variant="danger")

            @backend.branch
            def decision(approve, deny):
                return "approved" if approve else "rejected"

            approve >> decision(approve, deny)
            return (
                displays.Markdown("# Big page-sized report"),
                (approve, deny),
            )

        @node
        def approved():
            return (displays.Markdown("approved"),)

        @node
        def rejected():
            return (displays.Markdown("rejected"),)

        ln = landing()
        rv = review()
        ap = approved()
        rj = rejected()
        ln >> rv >> ap
        rv >> rj

    fm()
    return WORKFLOWS[form_id]


class TestPageWithInnerBranch:
    def test_page_body_supports_chain_backend(self):
        """A `@backend.branch` inside a flat-`@page` body must compile
        without the workflow-scope `steps.<x>` arg-type error."""
        wf = _build_form_with_page_branch("page_branch_compile")
        cw = compile_workflow(wf)
        # The implicit node's chain contains the branch decision.
        review_step = cw.by_id["review"]
        assert isinstance(review_step, CompiledPage)
        assert review_step.is_flat
        implicit = review_step.nodes[0]
        branch_calls = [
            cs.backend_call
            for cs in implicit.chain
            if cs.kind == "backend_call"
            and cs.backend_call.fn.is_branch
        ]
        assert len(branch_calls) == 1
        assert branch_calls[0].fn.name == "decision"

    def test_flat_page_with_branch_passes_fanout_validation(self):
        """A flat page whose implicit node hosts a branch counts as a
        branching step at the workflow level — `_step_is_branch` must
        recognise this so the edge validator allows fan-out to
        multiple downstream steps."""
        wf = _build_form_with_page_branch("page_branch_fanout")
        cw = compile_workflow(wf)
        review_step = cw.by_id["review"]
        assert _step_is_branch(review_step), (
            "flat @page with inner @backend.branch must report as "
            "a branching step so fan-out is allowed"
        )
        # And the downstream wiring stuck.
        assert set(review_step.downstream) == {"approved", "rejected"}

    def test_sectioned_page_unaffected(self):
        """A `@page` with `@node` sections is a different beast — its
        sections each set their own `_building_node`, and the page
        itself doesn't branch at the workflow level. The fix mustn't
        accidentally make sectioned pages look like branches."""
        @form(form_id="sectioned_page", title="x")
        def fm():
            @page
            def multi():
                @node
                def section_a():
                    return inputs.Text(id="a", label="a"), Button("Next")
                @node
                def section_b():
                    return inputs.Text(id="b", label="b"), Button("Done")
                section_a() >> section_b()

            @node
            def done():
                return (displays.Markdown("done"),)

            mp = multi()
            dn = done()
            mp >> dn

        fm()
        cw = compile_workflow(WORKFLOWS["sectioned_page"])
        multi_step = cw.by_id["multi"]
        assert isinstance(multi_step, CompiledPage)
        assert not multi_step.is_flat
        # No inner branch → not a branch step at the workflow level.
        assert not _step_is_branch(multi_step)

    def test_page_branch_routes_at_runtime(self):
        """Round-trip through the runtime: click approve, expect
        the submission to advance to the approved node. The bug
        previously was `_determine_next` walking the chain for a
        branch backend ONLY when downstream came from `step_def.
        downstream`, not when it came from the page's workflow
        edges. So a flat-page implicit node's inner branch was
        never consulted at routing time — the runtime saw multi-
        downstream-no-branch and raised."""
        from frontflow.dsl import runtime

        wf = _build_form_with_page_branch("page_branch_runtime")
        cw = compile_workflow(wf)
        sub = runtime.start_submission(cw, {"name": "Connor"})
        runtime.advance(cw, sub)
        runtime.submit_step(
            cw, sub, "review", {}, button_clicked="approve",
        )
        runtime.advance(cw, sub)
        assert not sub.failed, (
            f"submission unexpectedly failed: "
            f"{[(s.node_id, s.error) for s in sub.steps if s.error]}"
        )
        assert sub.steps[-1].node_id == "approved"
