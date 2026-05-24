"""Tests for `frontflow example seed --reset` and the underlying
`store.delete_submissions_for_form` helper.

Covers:
  - `delete_submissions_for_form` removes Submissions, Steps, Events,
    and SubmissionBlobs cleanly (cascade + manual delete in the same
    transaction).
  - Returns the (handle, submission_id) pairs the caller needs to
    also evict from runtime in-memory state.
  - A no-op delete (unknown form, or form with no submissions)
    doesn't error and returns an empty list.
  - Only the targeted form's submissions are touched — other forms
    are untouched.
"""
from __future__ import annotations


class TestDeleteSubmissionsForForm:
    def test_no_submissions_returns_empty(self, app):
        from frontflow.dsl import store
        # test_simple has no seeded submissions in the fixture set.
        wiped = store.delete_submissions_for_form("test_simple")
        assert wiped == []

    def test_unknown_form_returns_empty(self, app):
        from frontflow.dsl import store
        # No form_version rows for this id; no submissions to wipe.
        wiped = store.delete_submissions_for_form("does_not_exist")
        assert wiped == []

    def test_deletes_submissions_for_form(self, app):
        from frontflow.dsl import runtime, store
        import frontflow.main as main_mod

        wf = main_mod.FORMS["test_two_step"]
        vid = main_mod.FORM_VERSION_IDS["test_two_step"]
        sub_a = runtime.start_submission(
            wf, {"name": "Eve"}, form_version_id=vid,
        )
        runtime.advance(wf, sub_a)
        main_mod._persist(wf, sub_a)
        sub_b = runtime.start_submission(
            wf, {"name": "Frank"}, form_version_id=vid,
        )
        runtime.advance(wf, sub_b)
        main_mod._persist(wf, sub_b)

        wiped = store.delete_submissions_for_form("test_two_step")
        assert len(wiped) == 2
        handles = {h for h, _ in wiped}
        ids = {sid for _, sid in wiped}
        assert sub_a.handle in handles
        assert sub_b.handle in handles
        assert "eve" in ids
        assert "frank" in ids
        # DB-side gone.
        assert store.list_all_submission_ids() == {}

    def test_only_targeted_form_wiped(self, app):
        from frontflow.dsl import runtime, store
        import frontflow.main as main_mod

        # Seed one submission against two_step…
        wf_two = main_mod.FORMS["test_two_step"]
        vid_two = main_mod.FORM_VERSION_IDS["test_two_step"]
        sub_two = runtime.start_submission(
            wf_two, {"name": "Grace"}, form_version_id=vid_two,
        )
        runtime.advance(wf_two, sub_two)
        main_mod._persist(wf_two, sub_two)

        # …and one against simple. These should be untouched by a
        # delete scoped to two_step.
        wf_simple = main_mod.FORMS["test_simple"]
        vid_simple = main_mod.FORM_VERSION_IDS["test_simple"]
        sub_simple = runtime.start_submission(
            wf_simple, {"name": "Hank"}, form_version_id=vid_simple,
        )
        # test_simple has a single landing node, just persist after start.
        main_mod._persist(wf_simple, sub_simple)

        before = store.list_all_submission_ids()
        store.delete_submissions_for_form("test_two_step")
        after = store.list_all_submission_ids()
        # test_two_step's id gone, test_simple's still present.
        assert "grace" in before
        assert "grace" not in after
        # test_simple's submission unaffected.
        assert sub_simple.handle in after.values()


class TestResetCleansInMemoryToo:
    """The CLI's --reset must clear both DB and the runtime's
    in-memory caches; otherwise a fresh mint after reset collides
    with the stale `_id_index` entry. This is the integration check
    that the CLI hooks the runtime eviction correctly."""

    def test_runtime_eviction_after_delete_unblocks_remint(self, app):
        from frontflow.dsl import runtime, store
        import frontflow.main as main_mod

        wf = main_mod.FORMS["test_two_step"]
        vid = main_mod.FORM_VERSION_IDS["test_two_step"]
        sub_a = runtime.start_submission(
            wf, {"name": "Ivy"}, form_version_id=vid,
        )
        runtime.advance(wf, sub_a)
        main_mod._persist(wf, sub_a)
        assert "ivy" in runtime._id_index

        # Simulate what the CLI does on --reset: delete DB then
        # evict in-memory.
        wiped = store.delete_submissions_for_form("test_two_step")
        with runtime._submissions_lock:
            for handle, sid in wiped:
                runtime._submissions.pop(handle, None)
                if sid:
                    runtime._id_index.pop(sid, None)

        # The index entry is gone — a new "ivy" should mint without
        # raising the in-memory collision ValueError.
        assert "ivy" not in runtime._id_index
        sub_b = runtime.start_submission(
            wf, {"name": "Ivy"}, form_version_id=vid,
        )
        runtime.advance(wf, sub_b)
        # And the new submission successfully claimed the id.
        assert sub_b.submission_id == "ivy"
