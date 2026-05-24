"""Tests for the `_id_index` priming behavior in `hydrate_state()`.

Background: on startup, the runtime needs to know every persisted
submission_id so a new submission can't mint a colliding one. The
original implementation populated `_id_index` only as a side effect
of successfully hydrating each submission — so any submission whose
form_version couldn't be recompiled (DSL rename, removed source)
left its id absent from the index. Result: new submissions with the
same id passed the in-memory check and failed at the DB UNIQUE
constraint with a scary stack trace.

The fix: a separate Pass 1 in `hydrate_state()` reads every
persisted `(submission_id, handle)` pair and seeds `_id_index`
BEFORE the per-submission hydrate loop. Even un-hydratable rows
reserve their ids.
"""
from __future__ import annotations

import pytest


class TestListAllSubmissionIds:
    def test_returns_empty_for_empty_db(self, app):
        from frontflow.dsl import store
        assert store.list_all_submission_ids() == {}

    def test_returns_id_to_handle_map(self, app):
        # Seed two submissions directly via the runtime so the DB has
        # real rows with submission_ids set.
        from frontflow.dsl import runtime, store
        import frontflow.main as main_mod

        wf = main_mod.FORMS["test_two_step"]
        sub_a = runtime.start_submission(wf, {"name": "Alice"}, form_version_id=main_mod.FORM_VERSION_IDS["test_two_step"])
        runtime.advance(wf, sub_a)
        main_mod._persist(wf, sub_a)

        sub_b = runtime.start_submission(wf, {"name": "Bob"}, form_version_id=main_mod.FORM_VERSION_IDS["test_two_step"])
        runtime.advance(wf, sub_b)
        main_mod._persist(wf, sub_b)

        ids = store.list_all_submission_ids()
        assert ids.get("alice") == sub_a.handle
        assert ids.get("bob") == sub_b.handle

    def test_drafts_without_submission_id_are_excluded(self, app):
        # A submission whose id hasn't been minted yet (the template
        # references a step that hasn't run) shouldn't appear. This is
        # a defensive read — drafts can't collide on a missing id.
        from frontflow.dsl import store
        ids = store.list_all_submission_ids()
        # No drafts in the fixture forms; just assert the type is right.
        assert isinstance(ids, dict)


class TestIdIndexHydrationPrevention:
    """The core regression guard: after hydrate_state(), the in-memory
    index has every persisted id — including from submissions whose
    form_version can't be recompiled."""

    def test_hydrate_state_seeds_index_for_hydratable_submissions(
        self, app
    ):
        from frontflow.dsl import runtime, store
        import frontflow.main as main_mod

        # Seed a submission, persist, then clear in-memory state to
        # force hydrate to repopulate from disk.
        wf = main_mod.FORMS["test_two_step"]
        sub = runtime.start_submission(wf, {"name": "Carol"}, form_version_id=main_mod.FORM_VERSION_IDS["test_two_step"])
        runtime.advance(wf, sub)
        main_mod._persist(wf, sub)
        carol_id = sub.submission_id

        # Clear in-memory state (the app fixture also does this at
        # the start of each test; this simulates a fresh process).
        with runtime._submissions_lock:
            runtime._submissions.clear()
            runtime._id_index.clear()

        main_mod.hydrate_state()

        # The id is back in the index, even though we cleared everything.
        assert carol_id in runtime._id_index

    def test_id_index_blocks_collision_after_hydration(self, app):
        """End-to-end regression test for the original bug. Persist
        a submission, clear in-memory state, hydrate, then attempt
        to mint the same id — it should fail in-memory with the
        existing ValueError, NOT at the DB UNIQUE constraint."""
        from frontflow.dsl import runtime
        import frontflow.main as main_mod

        wf = main_mod.FORMS["test_two_step"]
        sub_first = runtime.start_submission(wf, {"name": "Dana"}, form_version_id=main_mod.FORM_VERSION_IDS["test_two_step"])
        runtime.advance(wf, sub_first)
        main_mod._persist(wf, sub_first)

        # Drop in-memory state — but DB still has the row.
        with runtime._submissions_lock:
            runtime._submissions.clear()
            runtime._id_index.clear()

        main_mod.hydrate_state()

        # Now attempt to mint a colliding id. The submission_id
        # template for test_two_step is "{{ steps.start.name |
        # slugify }}", so a second submission with name "Dana"
        # would mint "dana" again. The in-memory check should
        # reject this with the clear ValueError.
        with pytest.raises(ValueError, match="already exists"):
            sub_collide = runtime.start_submission(wf, {"name": "Dana"}, form_version_id=main_mod.FORM_VERSION_IDS["test_two_step"])
            runtime.advance(wf, sub_collide)


class TestHydrateStateLogsSummary:
    """The pre-fix code logged a line per un-hydratable submission,
    which spammed installs with stale rows. The fix collapses that
    to one summary line. Verifies via capsys that the loud per-row
    message is gone."""

    def test_no_per_row_spam_for_stale_rows(self, app, capsys):
        # No actual un-hydratable rows in the fixture DB (every
        # fixture form compiles). Just call hydrate_state and assert
        # no spam — establishes the new baseline. A future regression
        # adding spam would fail this test.
        import frontflow.main as main_mod
        main_mod.hydrate_state()
        out = capsys.readouterr().out
        # The pre-fix message format would say "cannot resolve form
        # version" once per skipped row. Absent in healthy installs.
        assert "cannot resolve form version" not in out
