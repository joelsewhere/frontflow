"""Tests for the per-user assignments listing + revocation endpoints.

Covers:
  - GET /api/users/{user_id}/assignments returns rows for that user
  - Includes form_id, form_title, role_id, granted_at, granter username
  - include_revoked=false filters to active rows
  - POST /api/assignments/{id}/revoke revokes a single row
  - Admin-only on both endpoints — non-admin gets 403
  - Already-revoked POST returns 404 (no double-revoke)
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from frontflow.dsl import assignments
from frontflow.dsl.store import User, _engine


def _user_id(username: str) -> int:
    with Session(_engine) as s:
        return s.execute(
            select(User).where(User.username == username)
        ).scalar_one().id


def _start_submission(client: TestClient, form_id: str) -> str:
    r = client.post(
        f"/api/forms/{form_id}/submissions",
        json={"button": None, "values": {"prompt": "x"}},
    )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    return body.get("handle") or body["submission_id"]


class TestListUserAssignments:
    def test_returns_active_assignments_for_user(
        self,
        admin_client: TestClient,
        user_client: TestClient,
        admin_user, regular_user,
    ):
        # Start a submission and grant the regular user a role.
        handle = _start_submission(user_client, "auth_open_form")
        bob_id = _user_id("user")
        admin_id = _user_id("admin")
        row = assignments.grant(
            submission_handle=handle,
            user_id=bob_id,
            role_id="approver",
            granted_by_user_id=admin_id,
        )

        # admin_client + user_client share a cookie jar — the
        # user_client login just above clobbered the admin cookie.
        # Re-login as admin so the admin-only endpoint passes.
        admin_client.post("/api/auth/login", json=admin_user)
        r = admin_client.get(f"/api/users/{bob_id}/assignments")
        assert r.status_code == 200
        rows = r.json()
        assert isinstance(rows, list)
        # At least the one we just granted is in there.
        matched = [a for a in rows if a["assignment_id"] == row.id]
        assert len(matched) == 1
        a = matched[0]
        assert a["submission_handle"] == handle
        assert a["role_id"] == "approver"
        assert a["granted_by_user_id"] == admin_id
        assert a["granted_by_username"] == "admin"
        assert a["form_id"] == "auth_open_form"
        # Form title may be the form_id when no display title is set,
        # but the field must be present.
        assert "form_title" in a
        # Active assignment — revoked_at must be null.
        assert a["revoked_at"] is None

    def test_include_revoked_false_filters_out_revoked(
        self,
        admin_client: TestClient,
        user_client: TestClient,
        admin_user, regular_user,
    ):
        handle = _start_submission(user_client, "auth_open_form")
        bob_id = _user_id("user")
        admin_id = _user_id("admin")
        row = assignments.grant(
            submission_handle=handle, user_id=bob_id,
            role_id="approver", granted_by_user_id=admin_id,
        )
        assignments.revoke(
            assignment_id=row.id, revoked_by_user_id=admin_id,
        )
        admin_client.post("/api/auth/login", json=admin_user)

        # Default (include_revoked=true): row appears with
        # revoked_at populated.
        r = admin_client.get(f"/api/users/{bob_id}/assignments")
        rows = r.json()
        revoked = [a for a in rows if a["assignment_id"] == row.id]
        assert len(revoked) == 1
        assert revoked[0]["revoked_at"] is not None
        assert revoked[0]["revoked_by_username"] == "admin"

        # With include_revoked=false: filtered out.
        r = admin_client.get(
            f"/api/users/{bob_id}/assignments?include_revoked=false"
        )
        rows = r.json()
        active = [a for a in rows if a["assignment_id"] == row.id]
        assert len(active) == 0

    def test_non_admin_denied(
        self,
        user_client: TestClient,
        admin_user, regular_user,
    ):
        bob_id = _user_id("user")
        r = user_client.get(f"/api/users/{bob_id}/assignments")
        # Admin-required: non-admin denied (status varies but is in
        # the 4xx range; the exact code depends on require_admin's
        # impl).
        assert r.status_code in (401, 403)


class TestRevokeAssignmentEndpoint:
    def test_admin_can_revoke_an_active_assignment(
        self,
        admin_client: TestClient,
        user_client: TestClient,
        admin_user, regular_user,
    ):
        handle = _start_submission(user_client, "auth_open_form")
        bob_id = _user_id("user")
        admin_id = _user_id("admin")
        row = assignments.grant(
            submission_handle=handle, user_id=bob_id,
            role_id="approver", granted_by_user_id=admin_id,
        )
        admin_client.post("/api/auth/login", json=admin_user)
        r = admin_client.post(
            f"/api/assignments/{row.id}/revoke"
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["assignment_id"] == row.id
        assert body["revoked_at"] is not None
        assert body["revoked_by_user_id"] == admin_id

    def test_revoke_an_already_revoked_returns_404(
        self,
        admin_client: TestClient,
        user_client: TestClient,
        admin_user, regular_user,
    ):
        handle = _start_submission(user_client, "auth_open_form")
        bob_id = _user_id("user")
        admin_id = _user_id("admin")
        row = assignments.grant(
            submission_handle=handle, user_id=bob_id,
            role_id="approver", granted_by_user_id=admin_id,
        )
        assignments.revoke(
            assignment_id=row.id, revoked_by_user_id=admin_id,
        )
        admin_client.post("/api/auth/login", json=admin_user)

        r = admin_client.post(f"/api/assignments/{row.id}/revoke")
        assert r.status_code == 404

    def test_non_admin_cannot_revoke(
        self,
        admin_client: TestClient,
        user_client: TestClient,
        admin_user, regular_user,
    ):
        handle = _start_submission(user_client, "auth_open_form")
        bob_id = _user_id("user")
        admin_id = _user_id("admin")
        row = assignments.grant(
            submission_handle=handle, user_id=bob_id,
            role_id="approver", granted_by_user_id=admin_id,
        )
        # user_client is the regular user (anon_client logged in as
        # them via the fixture). Revoke attempt should be denied.
        r = user_client.post(f"/api/assignments/{row.id}/revoke")
        assert r.status_code in (401, 403)
        # Assignment still active — verify by listing as admin
        # (must re-login since the user_client login clobbered).
        admin_client.post("/api/auth/login", json=admin_user)
        r2 = admin_client.get(f"/api/users/{bob_id}/assignments")
        active = [
            a for a in r2.json()
            if a["assignment_id"] == row.id and a["revoked_at"] is None
        ]
        assert len(active) == 1


class TestBulkRevokeOnSubmission:
    """`POST /api/submissions/{handle}/users/{user_id}/revoke-all` —
    flips every active assignment for one user on one submission in
    one call. Admin-only. Idempotent (200 + count=0 when nothing
    matches)."""

    def test_revokes_all_active_rows_for_user_on_submission(
        self,
        admin_client: TestClient,
        user_client: TestClient,
        admin_user, regular_user,
    ):
        handle = _start_submission(user_client, "auth_open_form")
        bob_id = _user_id("user")
        admin_id = _user_id("admin")
        # Three active grants on the same submission for one user.
        # All three should flip in one call. revoke_all_for_user_on
        # _submission is what's wired underneath.
        r1 = assignments.grant(
            submission_handle=handle, user_id=bob_id,
            role_id="approver", granted_by_user_id=admin_id,
        )
        r2 = assignments.grant(
            submission_handle=handle, user_id=bob_id,
            role_id="reviewer", granted_by_user_id=admin_id,
        )
        r3 = assignments.grant(
            submission_handle=handle, user_id=bob_id,
            role_id="signer", granted_by_user_id=admin_id,
        )

        # Re-login admin (conftest cookie-jar share).
        admin_client.post("/api/auth/login", json=admin_user)
        r = admin_client.post(
            f"/api/submissions/{handle}/users/{bob_id}/revoke-all"
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {"revoked_count": 3}

        # Verify all three rows are now revoked.
        r_list = admin_client.get(f"/api/users/{bob_id}/assignments")
        assert r_list.status_code == 200
        by_id = {a["assignment_id"]: a for a in r_list.json()}
        for ann_id in (r1.id, r2.id, r3.id):
            assert by_id[ann_id]["revoked_at"] is not None, (
                f"row {ann_id} not revoked after bulk call"
            )

    def test_leaves_other_users_grants_alone(
        self,
        admin_client: TestClient,
        user_client: TestClient,
        admin_user, regular_user, anon_client: TestClient,
    ):
        # Create a second non-admin user; both have grants on the
        # same submission. Bulk-revoke for user A must NOT touch
        # user B's grant.
        from frontflow.dsl import auth as _auth
        _auth.create_user("carol", "pw-12345678", is_admin=False)
        handle = _start_submission(user_client, "auth_open_form")
        bob_id = _user_id("user")
        carol_id = _user_id("carol")
        admin_id = _user_id("admin")
        a_row = assignments.grant(
            submission_handle=handle, user_id=bob_id,
            role_id="approver", granted_by_user_id=admin_id,
        )
        b_row = assignments.grant(
            submission_handle=handle, user_id=carol_id,
            role_id="approver", granted_by_user_id=admin_id,
        )

        admin_client.post("/api/auth/login", json=admin_user)
        r = admin_client.post(
            f"/api/submissions/{handle}/users/{bob_id}/revoke-all"
        )
        assert r.status_code == 200
        assert r.json() == {"revoked_count": 1}

        # Carol's grant still active.
        carol_list = admin_client.get(
            f"/api/users/{carol_id}/assignments"
        )
        carol_rows = carol_list.json()
        assert any(
            a["assignment_id"] == b_row.id and a["revoked_at"] is None
            for a in carol_rows
        ), f"Carol's grant was incorrectly revoked: {carol_rows}"
        # And Bob's flipped.
        bob_list = admin_client.get(f"/api/users/{bob_id}/assignments")
        bob_rows = bob_list.json()
        assert any(
            a["assignment_id"] == a_row.id and a["revoked_at"] is not None
            for a in bob_rows
        ), f"Bob's grant wasn't flipped: {bob_rows}"

    def test_idempotent_when_nothing_active(
        self,
        admin_client: TestClient,
        user_client: TestClient,
        admin_user, regular_user,
    ):
        # No grants at all — endpoint should still return 200 with
        # count=0. Same for an already-revoked one.
        handle = _start_submission(user_client, "auth_open_form")
        bob_id = _user_id("user")
        admin_client.post("/api/auth/login", json=admin_user)
        r = admin_client.post(
            f"/api/submissions/{handle}/users/{bob_id}/revoke-all"
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"revoked_count": 0}

        # Add a grant, revoke it manually, then bulk again — should
        # still be 0 (the bulk only flips ACTIVE rows).
        admin_id = _user_id("admin")
        row = assignments.grant(
            submission_handle=handle, user_id=bob_id,
            role_id="approver", granted_by_user_id=admin_id,
        )
        assignments.revoke(
            assignment_id=row.id, revoked_by_user_id=admin_id,
        )
        admin_client.post("/api/auth/login", json=admin_user)
        r = admin_client.post(
            f"/api/submissions/{handle}/users/{bob_id}/revoke-all"
        )
        assert r.status_code == 200
        assert r.json() == {"revoked_count": 0}

    def test_non_admin_cannot_bulk_revoke(
        self,
        admin_client: TestClient,
        user_client: TestClient,
        admin_user, regular_user,
    ):
        handle = _start_submission(user_client, "auth_open_form")
        bob_id = _user_id("user")
        admin_id = _user_id("admin")
        row = assignments.grant(
            submission_handle=handle, user_id=bob_id,
            role_id="approver", granted_by_user_id=admin_id,
        )
        # Regular (signed-in but non-admin) user — denied.
        r = user_client.post(
            f"/api/submissions/{handle}/users/{bob_id}/revoke-all"
        )
        assert r.status_code in (401, 403)
        # Grant still active.
        admin_client.post("/api/auth/login", json=admin_user)
        r2 = admin_client.get(f"/api/users/{bob_id}/assignments")
        assert any(
            a["assignment_id"] == row.id and a["revoked_at"] is None
            for a in r2.json()
        ), "non-admin bulk revoke leaked through"
