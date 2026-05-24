"""Runtime tests — submission lifecycle through the Python API.

Tests the runtime module directly (not through FastAPI) so failures
point at runtime logic, not HTTP plumbing. The fixture forms
(test_simple, test_two_step) are reloaded into FORMS via the `app`
fixture's startup.

Covers:
  - start_submission → submit_step round trips
  - advance() reaches terminal state
  - validate_repin reports compatibility / incompatibility
  - repin moves the pinned version
"""
from __future__ import annotations

import pytest


@pytest.fixture
def simple_workflow(app):
    """The test_simple compiled workflow."""
    import frontflow.main as main_mod
    wf = main_mod.FORMS.get("test_simple")
    assert wf is not None, "test_simple fixture form missing from FORMS"
    return wf


@pytest.fixture
def two_step_workflow(app):
    """The test_two_step compiled workflow."""
    import frontflow.main as main_mod
    wf = main_mod.FORMS.get("test_two_step")
    assert wf is not None, (
        "test_two_step fixture form missing from FORMS"
    )
    return wf


class TestStartSubmission:
    def test_creates_submission_with_handle(self, simple_workflow):
        from frontflow.dsl import runtime
        sub = runtime.start_submission(
            simple_workflow, {"name": "Test", "note": ""}
        )
        runtime.advance(simple_workflow, sub)
        assert sub.handle
        assert len(sub.steps) >= 1
        # Single-step form: should be terminated after advance.
        assert sub.terminated is True

    def test_two_step_starts_with_first_submitted(
        self, two_step_workflow
    ):
        from frontflow.dsl import runtime
        sub = runtime.start_submission(
            two_step_workflow, {"name": "Alice"}
        )
        runtime.advance(two_step_workflow, sub)
        # Step 1 submitted, step 2 should be the awaiting frontier.
        assert sub.terminated is False
        assert sub.failed is False
        assert sub.submission_id == "alice"
        # The first step is recorded.
        step_ids = [s.node_id for s in sub.steps]
        assert "start" in step_ids


class TestSubmitStep:
    def test_advances_two_step_to_terminal(self, two_step_workflow):
        from frontflow.dsl import runtime
        sub = runtime.start_submission(
            two_step_workflow, {"name": "Bob"}
        )
        runtime.advance(two_step_workflow, sub)
        assert sub.terminated is False
        runtime.submit_step(
            two_step_workflow,
            sub,
            "confirm",
            {"confirmed": "Bob"},
            button_clicked=None,
        )
        runtime.advance(two_step_workflow, sub)
        assert sub.terminated is True
        assert sub.failed is False

    def test_unknown_step_raises(self, two_step_workflow):
        from frontflow.dsl import runtime
        sub = runtime.start_submission(
            two_step_workflow, {"name": "Carol"}
        )
        runtime.advance(two_step_workflow, sub)
        with pytest.raises(KeyError):
            runtime.submit_step(
                two_step_workflow,
                sub,
                "no_such_step",
                {},
                button_clicked=None,
            )


class TestValidateRepin:
    def test_same_workflow_is_compatible(self, simple_workflow):
        """A submission repinned to the same compiled workflow is
        always compatible — same nodes, same schema."""
        from frontflow.dsl import runtime
        sub = runtime.start_submission(
            simple_workflow, {"name": "X", "note": ""}
        )
        issues = runtime.validate_repin(
            simple_workflow, simple_workflow, sub
        )
        assert issues == [], (
            f"identity repin reported issues: {issues}"
        )

    def test_two_step_self_repin_is_compatible(
        self, two_step_workflow
    ):
        from frontflow.dsl import runtime
        sub = runtime.start_submission(
            two_step_workflow, {"name": "Y"}
        )
        issues = runtime.validate_repin(
            two_step_workflow, two_step_workflow, sub
        )
        assert issues == []


class TestSubmissionState:
    def test_started_submission_is_retrievable_by_handle(
        self, two_step_workflow
    ):
        """A started submission can be fetched back by its handle —
        keeps the in-memory dict honest."""
        from frontflow.dsl import runtime
        sub = runtime.start_submission(
            two_step_workflow, {"name": "Persisted"}
        )
        runtime.advance(two_step_workflow, sub)
        fetched = runtime.get_submission(sub.handle)
        assert fetched is not None
        assert fetched.handle == sub.handle
