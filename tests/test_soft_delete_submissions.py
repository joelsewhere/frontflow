"""Tests for soft-deleting submissions via
`POST /api/forms/{form_id}/submissions/delete`.

The endpoint sets `deleted_at` on the matching rows rather than
removing them — Step / Event / SubmissionBlob audit trails stay
intact, but every user-facing read path filters tombstoned rows
out so they vanish from the UI.

This file pins:
  - The schema migration (the column exists, indexed).
  - The store function's semantics: scoped to form_id; returns
    (deleted, not_found) buckets; idempotent on re-issue.
  - The endpoint's admin gate (anon, regular user, admin).
  - The read-path filtering: load_submissions and the form
    listing both skip tombstoned rows.
  - Runtime cache eviction so a deleted submission's id can't be
    reached via the in-memory `_submissions` / `_id_index` maps.
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import inspect

from frontflow.dsl import runtime
import frontflow.dsl.store as store
from frontflow.dsl.store import (
    Session,
    Submission,
    _engine,
    load_submissions,
    soft_delete_submissions_by_handles,
)


# --- Helpers ----------------------------------------------------------------


def _start_simple_submission(client: TestClient, name: str) -> str:
    """Create a submission on the `test_simple` fixture form and
    return its handle. The fixture form is one node; the submission
    is born already terminal."""
    r = client.post(
        "/api/forms/test_simple/submissions",
        json={"values": {"name": name, "note": "ok"}},
    )
    assert r.status_code == 201, r.text
    return r.json()["handle"]


# --- Schema -----------------------------------------------------------------


class TestSchemaMigration:
    def test_deleted_at_column_exists(self, app):
        """The migration that adds `deleted_at` ran during the
        test's `init_db`. Without the column, every read path's
        WHERE clause would raise instead of degrading to a no-op."""
        cols = {
            c["name"] for c in inspect(_engine).get_columns("submission")
        }
        assert "deleted_at" in cols

    def test_deleted_at_is_indexed(self, app):
        """Indexed because every listing query filters on it; an
        unindexed scan would be a hot-path regression on prod-sized
        tables."""
        indexes = inspect(_engine).get_indexes("submission")
        col_lists = [tuple(i["column_names"]) for i in indexes]
        assert ("deleted_at",) in col_lists


# --- Store function ---------------------------------------------------------


class TestSoftDeleteStoreFn:
    def test_sets_deleted_at_and_returns_pair(
        self, app, admin_client: TestClient,
    ):
        handle = _start_simple_submission(admin_client, "soft-store-1")
        deleted, not_found = soft_delete_submissions_by_handles(
            [handle], form_id="test_simple",
        )
        assert not_found == []
        assert len(deleted) == 1
        assert deleted[0][0] == handle
        # The DB row survives; only `deleted_at` is populated.
        with Session(_engine) as s:
            sub = s.get(Submission, handle)
            assert sub is not None
            assert sub.deleted_at is not None

    def test_unknown_handle_lands_in_not_found(
        self, app, admin_client: TestClient,
    ):
        deleted, not_found = soft_delete_submissions_by_handles(
            ["does-not-exist"], form_id="test_simple",
        )
        assert deleted == []
        assert not_found == ["does-not-exist"]

    def test_cross_form_handle_is_not_found(
        self, app, admin_client: TestClient,
    ):
        """A handle that belongs to form A but is passed against
        form B's URL must be rejected (treated as not_found). The
        admin gate alone is not enough — scope check is defense in
        depth so a slip-up in URL construction can't trash an
        adjacent form's data."""
        handle = _start_simple_submission(admin_client, "cross-form")
        # `test_two_step` exists in the fixture set; the handle is
        # on `test_simple` and must not resolve under it.
        deleted, not_found = soft_delete_submissions_by_handles(
            [handle], form_id="test_two_step",
        )
        assert deleted == []
        assert not_found == [handle]
        # The handle is still live on its real form — the scope
        # rejection didn't leak into the other form's state.
        with Session(_engine) as s:
            sub = s.get(Submission, handle)
            assert sub.deleted_at is None

    def test_already_deleted_is_idempotent_and_not_found(
        self, app, admin_client: TestClient,
    ):
        """A second delete of the same handle returns it as
        not_found rather than re-stamping. Re-stamping would change
        an audit timestamp the user assumes is the original delete
        moment."""
        handle = _start_simple_submission(admin_client, "soft-idem")
        soft_delete_submissions_by_handles(
            [handle], form_id="test_simple",
        )
        with Session(_engine) as s:
            first_stamp = s.get(Submission, handle).deleted_at
        deleted, not_found = soft_delete_submissions_by_handles(
            [handle], form_id="test_simple",
        )
        assert deleted == []
        assert not_found == [handle]
        with Session(_engine) as s:
            assert s.get(Submission, handle).deleted_at == first_stamp

    def test_partial_batch_splits_into_buckets(
        self, app, admin_client: TestClient,
    ):
        live = _start_simple_submission(admin_client, "soft-batch-1")
        deleted, not_found = soft_delete_submissions_by_handles(
            [live, "ghost"], form_id="test_simple",
        )
        assert {h for h, _ in deleted} == {live}
        assert not_found == ["ghost"]


# --- Read-path filtering ----------------------------------------------------


class TestSoftDeletedHidden:
    def test_load_submissions_skips_tombstoned(
        self, app, admin_client: TestClient,
    ):
        """Boot rehydration must not re-introduce a deleted
        submission into the runtime's in-memory map."""
        handle = _start_simple_submission(admin_client, "soft-load-1")
        soft_delete_submissions_by_handles(
            [handle], form_id="test_simple",
        )
        snaps = load_submissions()
        assert all(s["handle"] != handle for s in snaps)

    def test_list_form_submissions_skips_tombstoned(
        self, app, admin_client: TestClient,
    ):
        """The listing the SubmissionsTab calls must drop deleted
        rows from its result."""
        keep = _start_simple_submission(admin_client, "soft-list-keep")
        drop = _start_simple_submission(admin_client, "soft-list-drop")
        soft_delete_submissions_by_handles(
            [drop], form_id="test_simple",
        )
        page = store.list_form_submissions("test_simple")
        handles = {r["handle"] for r in page["submissions"]}
        assert keep in handles
        assert drop not in handles


# --- Endpoint ---------------------------------------------------------------


class TestEndpointAuthAndSemantics:
    def test_anon_cannot_call_endpoint(
        self, app, anon_client: TestClient, admin_user: dict,
    ):
        """No session cookie → 401 (require_admin's "auth required"
        bucket). The endpoint is admin-only by design — we don't
        want submission owners deleting their own data without
        admin oversight.

        `admin_user` is requested but never used directly — it's
        the fixture that bootstraps an admin account so the gate
        falls through "no users exist → 503" to the real "no
        session → 401" branch we're testing here."""
        del admin_user  # bootstrap-only, see docstring
        r = anon_client.post(
            "/api/forms/test_simple/submissions/delete",
            json={"handles": ["anything"]},
        )
        assert r.status_code == 401

    def test_regular_user_is_forbidden(
        self, app, user_client: TestClient,
    ):
        """Authenticated non-admin → 403, not 401. The auth
        succeeded; the gate is the admin flag."""
        r = user_client.post(
            "/api/forms/test_simple/submissions/delete",
            json={"handles": ["anything"]},
        )
        assert r.status_code == 403

    def test_admin_can_delete_and_returns_partition(
        self, app, admin_client: TestClient,
    ):
        live = _start_simple_submission(admin_client, "soft-ep-1")
        r = admin_client.post(
            "/api/forms/test_simple/submissions/delete",
            json={"handles": [live, "no-such-handle"]},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["deleted"] == [live]
        assert body["not_found"] == ["no-such-handle"]

    def test_in_memory_caches_are_evicted_on_delete(
        self, app, admin_client: TestClient,
    ):
        """A tombstoned submission must not remain reachable via
        the runtime's `_submissions` / `_id_index` maps — both
        because subsequent advance/persist calls on a stale cache
        entry would resurrect timestamps, and because the cached
        id-index entry would block re-use of a deterministic
        submission_id."""
        live = _start_simple_submission(admin_client, "soft-ep-mem")
        # Sanity: it's in the cache pre-delete.
        with runtime._submissions_lock:
            assert live in runtime._submissions

        r = admin_client.post(
            "/api/forms/test_simple/submissions/delete",
            json={"handles": [live]},
        )
        assert r.status_code == 200, r.text

        # Post-delete: gone from both maps.
        with runtime._submissions_lock:
            assert live not in runtime._submissions
            assert live not in runtime._id_index.values()
