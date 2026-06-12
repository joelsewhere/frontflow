"""Tests for `selective_force_repin_submission` — the truncation
behavior that keeps the longest valid prefix of a submission's
chain and drops only the steps invalidated by the structural
change.

Three scenarios:
  1. No tainted steps — falls through to a clean repin (no truncation).
  2. Mid-chain node invalidated — kept prefix, dropped suffix.
  3. First step invalidated — everything drops, submission resumes
     at the live form's landing node (since the first dropped step's
     node may not exist on the new version either).
"""
from __future__ import annotations

import pytest


@pytest.fixture
def two_step(app):
    import frontflow.main as main_mod
    wf = main_mod.FORMS.get("test_two_step")
    assert wf is not None
    return wf


def _drop_node(wf, node_id):
    """Return a shallow clone of `wf` where `node_id` is missing from
    all_nodes_by_id. Simulates a structural change where that node
    was removed from the live form. We mutate a copy rather than
    `wf` itself because FORMS is shared across tests."""
    from copy import copy
    live = copy(wf)
    live.all_nodes_by_id = {
        nid: n for nid, n in wf.all_nodes_by_id.items() if nid != node_id
    }
    # `landing_node` is a method on CompiledWorkflow that returns the
    # entry node. selective_force_repin_submission calls it as a
    # fallback. The copy inherits the bound method which still
    # references the original `wf`'s nodes — that's fine for our
    # tests; landing_node() returns the same `start` node.
    return live


def _submit_start(wf, fields):
    """Helper: start a submission AND advance so the start step is
    in the submitted state (otherwise validate_repin ignores it)."""
    from frontflow.dsl import runtime
    sub = runtime.start_submission(wf, fields)
    runtime.advance(wf, sub)
    return sub


class TestSelectiveForceRepin:
    def test_no_tainted_steps_falls_through_to_clean_repin(self, two_step):
        from frontflow.dsl import runtime
        sub = _submit_start(two_step, {"name": "ok"})
        # Repinning to itself — no invalidation possible.
        result = runtime.selective_force_repin_submission(
            two_step, two_step, sub, new_version_id=999,
        )
        assert result["dropped"] == []
        assert sub.form_version_id == 999
        assert "submission_repinned" in [
            e.type for e in sub.events
        ]

    def test_drops_invalidated_step_and_resumes_at_landing(self, two_step):
        """If the only submitted step's node is gone in `live`, the
        chain truncates and the submission resumes at the live form's
        landing node (resume_node lookup falls back because the
        dropped step's node isn't in `live`)."""
        from frontflow.dsl import runtime
        sub = _submit_start(two_step, {"name": "casualty"})
        live = _drop_node(two_step, "start")
        result = runtime.selective_force_repin_submission(
            two_step, live, sub, new_version_id=999,
        )
        # `start` is invalidated; `confirm` follows in the chain, so
        # both drop. The truncation rule is "everything from the
        # first tainted step onward".
        assert result["dropped"] == ["start", "confirm"]
        assert result["kept"] == []
        assert sub.form_version_id == 999
        assert sub.terminated is False
        assert sub.failed is False
        sel_evt = next(
            (e for e in sub.events
             if e.type == "submission_selective_force_repinned"),
            None,
        )
        assert sel_evt is not None
        assert sel_evt.payload["dropped_steps"] == ["start", "confirm"]
        assert sel_evt.payload["kept_steps"] == []
        assert sel_evt.payload["to_version"] == 999

    def test_idempotent_when_called_repeatedly_after_truncation(self, two_step):
        from frontflow.dsl import runtime
        sub = _submit_start(two_step, {"name": "stable"})
        runtime.selective_force_repin_submission(
            two_step, two_step, sub, new_version_id=999,
        )
        result = runtime.selective_force_repin_submission(
            two_step, two_step, sub, new_version_id=1000,
        )
        assert result["dropped"] == []
        assert sub.form_version_id == 1000
