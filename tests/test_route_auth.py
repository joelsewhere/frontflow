"""Tests for runtime auth check wired into HTTP routes.

Covers:
  - read_step surfaces `access` payload
  - Open-mode forms (no roles) return fully-permitted access
  - Role-gated nodes return pending=True for users with no
    matching role, with the layout stripped to a placeholder
  - Admins always get full access
  - Non-admin with matching role gets full access
  - submit_step_endpoint returns 403 when user can't write
  - submit_step_endpoint returns 403 when user has read-only
    access on the node
  - Per-input role= strips submitted values from non-permitted
    users (the runtime never sees them)
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from frontflow import (
    Button, Role, form, inputs, node, users,
)
from frontflow.dsl import assignments
from frontflow.dsl.core import WORKFLOWS
from frontflow.dsl.store import User, _engine


def _user_id(username: str) -> int:
    with Session(_engine) as s:
        return s.execute(
            select(User).where(User.username == username)
        ).scalar_one().id


def _clear_cookies(client: TestClient) -> None:
    """Clear the shared cookie jar — the conftest fixtures all use
    the same FastAPI app so an earlier admin login bleeds over.
    Anonymous tests must explicitly drop cookies."""
    client.cookies.clear()


@pytest.fixture
def open_form(app):
    """The 'auth_open_form' fixture — no roles, anyone can use it."""
    return "auth_open_form"


@pytest.fixture
def role_gated_form(app):
    """The 'auth_gated_form' fixture — step1 gated to 'approver'."""
    return "auth_gated_form"


@pytest.fixture
def per_input_role_form(app):
    """The 'auth_per_input_form' fixture — per-input roles."""
    return "auth_per_input_form"


def _start_submission(
    client: TestClient, form_id: str, *, advance_to: str = "step1",
) -> str:
    """Hit the landing endpoint and return the submission handle.

    The landing POST auto-submits the form's landing node. For
    `auth_open_form` (single node), that's step1. For
    `auth_gated_form` / `auth_per_input_form` (step1 >> step2),
    the landing POST also auto-advances to step2 by submitting
    step1 — so the caller can read/post against step2 directly.
    `advance_to` is documentation only; the runtime decides which
    step is next.
    """
    r = client.post(
        f"/api/forms/{form_id}/submissions",
        json={"button": None, "values": {"prompt": "x"}},
    )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    return body.get("handle") or body["submission_id"]


# ----------------------------------------------------------------------------
# read_step access payload
# ----------------------------------------------------------------------------

class TestReadStepAccess:
    def test_open_form_returns_permitted_access(
        self, admin_client: TestClient, open_form,
    ):
        handle = _start_submission(admin_client, open_form)
        r = admin_client.get(
            f"/api/forms/{open_form}/submissions/{handle}/steps/step1"
        )
        # Even for an open-mode form, the access payload exists.
        # On open-mode forms, the response defaults to fully-permitted.
        assert r.status_code == 200
        body = r.json()
        assert "access" in body
        access = body["access"]
        assert access["can_read"] is True
        assert access["can_write"] is True
        assert access["pending"] is False

    def test_admin_gets_full_access_on_gated_form(
        self, admin_client: TestClient, role_gated_form,
    ):
        handle = _start_submission(admin_client, role_gated_form, advance_to="step2")
        r = admin_client.get(
            f"/api/forms/{role_gated_form}/submissions/{handle}/steps/step2"
        )
        assert r.status_code == 200
        access = r.json()["access"]
        assert access["can_read"] is True
        assert access["can_write"] is True
        assert access["pending"] is False

    def test_non_admin_no_role_renders_pending(
        self,
        admin_client: TestClient,
        user_client: TestClient,
        role_gated_form, admin_user, regular_user,
    ):
        # Admin starts the submission so we have a handle that the
        # user can reach (the form is public-visibility by default).
        handle = _start_submission(admin_client, role_gated_form, advance_to="step2")
        r = user_client.get(
            f"/api/forms/{role_gated_form}/submissions/{handle}/steps/step2"
        )
        assert r.status_code == 200
        access = r.json()["access"]
        assert access["can_read"] is False
        assert access["can_write"] is False
        assert access["pending"] is True
        assert access["missing_write_roles"] == ["approver"]

    def test_pending_strips_layout_to_placeholder(
        self,
        admin_client: TestClient,
        user_client: TestClient,
        role_gated_form, admin_user, regular_user,
    ):
        handle = _start_submission(admin_client, role_gated_form, advance_to="step2")
        r = user_client.get(
            f"/api/forms/{role_gated_form}/submissions/{handle}/steps/step2"
        )
        body = r.json()
        # The pending placeholder doesn't carry the real Decision
        # input — its label/options shouldn't leak.
        import json
        payload = json.dumps(body)
        assert "Decision" not in payload
        assert "Yes" not in payload
        assert "No" not in payload

    def test_non_admin_with_role_gets_full_access(
        self,
        admin_client: TestClient,
        user_client: TestClient,
        role_gated_form, admin_user, regular_user,
    ):
        handle = _start_submission(admin_client, role_gated_form, advance_to="step2")
        # Grant the regular user the approver role on this submission.
        assignments.grant(
            submission_handle=handle,
            user_id=_user_id("user"),
            role_id="approver",
            granted_by_user_id=_user_id("admin"),
        )
        r = user_client.get(
            f"/api/forms/{role_gated_form}/submissions/{handle}/steps/step2"
        )
        access = r.json()["access"]
        assert access["can_read"] is True
        assert access["can_write"] is True
        assert access["pending"] is False


# ----------------------------------------------------------------------------
# submit_step_endpoint enforcement
# ----------------------------------------------------------------------------

class TestSubmitStepEnforcement:
    def test_admin_can_submit_gated_step(
        self, admin_client: TestClient, role_gated_form,
    ):
        handle = _start_submission(admin_client, role_gated_form, advance_to="step2")
        r = admin_client.post(
            f"/api/forms/{role_gated_form}/submissions/{handle}/steps/step2",
            json={"button": None, "values": {"decision": "Yes"}},
        )
        # 200 means the submit went through (no auth block).
        assert r.status_code == 200, r.text

    def test_non_admin_without_role_403(
        self,
        admin_client: TestClient,
        user_client: TestClient,
        role_gated_form, admin_user, regular_user,
    ):
        handle = _start_submission(admin_client, role_gated_form, advance_to="step2")
        r = user_client.post(
            f"/api/forms/{role_gated_form}/submissions/{handle}/steps/step2",
            json={"button": None, "values": {"decision": "Yes"}},
        )
        assert r.status_code == 403
        # Error message mentions the missing role.
        assert "approver" in r.text or "assigned" in r.text

    def test_non_admin_with_role_can_submit(
        self,
        admin_client: TestClient,
        user_client: TestClient,
        role_gated_form, admin_user, regular_user,
    ):
        handle = _start_submission(admin_client, role_gated_form, advance_to="step2")
        assignments.grant(
            submission_handle=handle,
            user_id=_user_id("user"),
            role_id="approver",
            granted_by_user_id=_user_id("admin"),
        )
        r = user_client.post(
            f"/api/forms/{role_gated_form}/submissions/{handle}/steps/step2",
            json={"button": None, "values": {"decision": "Yes"}},
        )
        assert r.status_code == 200, r.text


# ----------------------------------------------------------------------------
# Per-input role= stripping
# ----------------------------------------------------------------------------


class TestPerInputRoleStripping:
    def test_user_with_only_requester_role_cannot_set_decision(
        self,
        admin_client: TestClient,
        user_client: TestClient,
        per_input_role_form, admin_user, regular_user,
    ):
        handle = _start_submission(admin_client, per_input_role_form, advance_to="step2")
        # Grant requester only — NOT approver.
        assignments.grant(
            submission_handle=handle,
            user_id=_user_id("user"),
            role_id="requester",
            granted_by_user_id=_user_id("admin"),
        )
        # Submit with BOTH fields — the decision field should be
        # stripped since the user doesn't hold approver.
        r = user_client.post(
            f"/api/forms/{per_input_role_form}/submissions/{handle}/steps/step2",
            json={
                "button": None,
                "values": {
                    "summary": "My summary",
                    "decision": "Yes",   # user can't write this
                },
            },
        )
        assert r.status_code == 200, r.text
        # The persisted values should NOT include decision.
        body = r.json()
        values = (body.get("response") or {}).get("values", {})
        assert values.get("summary") == "My summary"
        assert "decision" not in values


# ----------------------------------------------------------------------------
# Per-submission visibility gate (Phase 7)
# ----------------------------------------------------------------------------
#
# `can_view_submission` accepts five contributor paths:
#   - admin
#   - folder grant on the form's folder
#   - step submitter (user_id stamped on at least one step)
#   - active assignee (SubmissionAssignment with revoked_at IS NULL)
#   - granter of an assignment on this submission
#
# Each test isolates one path; one negative test confirms a user with
# none of these grants is denied. The gate fires on three endpoints:
#   - GET /forms/{id}/submissions/{id}
#   - GET /forms/{id}/submissions/{id}/detail
#   - GET /forms/{id}/submissions/{id}/steps/{step_id}
# Each test checks one of the three; the gate is the same helper, so
# coverage on one endpoint demonstrates the gate works on all of them.


class TestSubmissionVisibilityGate:
    """Phase 7: viewing a submission requires being a contributor.

    Forms remain public-by-default for filling; submissions become
    private-by-default for viewing. Specifically: even on a public
    form, only the submission's contributors / assignees / granters
    / folder-grant-holders / admins can see what got submitted.
    """

    @pytest.fixture
    def make_extra_user(self, anon_client: TestClient):
        """Factory: create a fresh non-admin user beyond the
        conftest's `regular_user`. Returns the username, which
        tests pass to `_user_id` to resolve the id."""
        from frontflow.dsl import auth as _auth

        created: list[str] = []

        def _make(username: str) -> str:
            _auth.create_user(
                username, "pw-12345678", is_admin=False,
            )
            created.append(username)
            return username
        yield _make

    @pytest.fixture
    def bob_client(
        self, app, make_extra_user, anon_client: TestClient,
    ) -> TestClient:
        """An authenticated, non-admin, non-contributor user.
        Used as the negative case across the gate tests."""
        make_extra_user("bob")
        # Login on a fresh client so this user's cookie doesn't
        # collide with the conftest's admin/user clients.
        from fastapi.testclient import TestClient as _TC
        client = _TC(app, raise_server_exceptions=False)
        r = client.post(
            "/api/auth/login",
            json={"username": "bob", "password": "pw-12345678"},
        )
        assert r.status_code == 200
        yield client
        client.close()

    def test_admin_can_see_anyone_else_submission(
        self,
        admin_client: TestClient,
        user_client: TestClient,
        admin_user, regular_user, open_form,
    ):
        # Regular user submits; admin should be able to view it.
        handle = _start_submission(user_client, open_form)
        r = admin_client.get(
            f"/api/forms/{open_form}/submissions/{handle}"
        )
        assert r.status_code == 200

    def test_step_submitter_can_view_own_submission(
        self,
        user_client: TestClient,
        regular_user, open_form,
    ):
        # The user who started/submitted this submission must see it.
        handle = _start_submission(user_client, open_form)
        r = user_client.get(
            f"/api/forms/{open_form}/submissions/{handle}"
        )
        assert r.status_code == 200

    def test_non_contributor_cannot_view_submission(
        self,
        user_client: TestClient,
        bob_client: TestClient,
        admin_user, regular_user, open_form,
    ):
        # Regular user submits. Bob — different user, no grant,
        # no assignment — must NOT see the submission.
        handle = _start_submission(user_client, open_form)
        r = bob_client.get(
            f"/api/forms/{open_form}/submissions/{handle}"
        )
        assert r.status_code == 404, (
            f"non-contributor reached the submission "
            f"(status={r.status_code}, body={r.text})"
        )

    def test_assignee_can_view_assigned_submission(
        self,
        user_client: TestClient,
        bob_client: TestClient,
        admin_user, regular_user, open_form, make_extra_user,
    ):
        # Regular user submits. Bob isn't a contributor — but if
        # we grant Bob an active assignment on this submission, he
        # should be permitted.
        handle = _start_submission(user_client, open_form)
        bob_id = _user_id("bob")
        assignments.grant(
            submission_handle=handle,
            user_id=bob_id,
            role_id="approver",
            granted_by_user_id=_user_id("user"),
        )
        r = bob_client.get(
            f"/api/forms/{open_form}/submissions/{handle}"
        )
        assert r.status_code == 200, (
            f"active assignee was denied (status={r.status_code}, "
            f"body={r.text})"
        )

    def test_granter_can_view_submission_they_granted_on(
        self,
        user_client: TestClient,
        bob_client: TestClient,
        admin_user, regular_user, open_form,
    ):
        # Bob grants an assignment on a submission to the regular
        # user. Bob is now a granter on that submission and should
        # be able to view it, even though he didn't fill any step.
        handle = _start_submission(user_client, open_form)
        bob_id = _user_id("bob")
        assignments.grant(
            submission_handle=handle,
            user_id=_user_id("user"),
            role_id="approver",
            granted_by_user_id=bob_id,
        )
        r = bob_client.get(
            f"/api/forms/{open_form}/submissions/{handle}"
        )
        assert r.status_code == 200, (
            f"granter was denied (status={r.status_code}, "
            f"body={r.text})"
        )

    def test_revoked_assignee_loses_visibility(
        self,
        user_client: TestClient,
        bob_client: TestClient,
        admin_user, regular_user, open_form,
    ):
        # Bob was assigned, then revoked. He must not see it.
        # (Today's assignments.grant + assignments.revoke flow.)
        handle = _start_submission(user_client, open_form)
        bob_id = _user_id("bob")
        row = assignments.grant(
            submission_handle=handle,
            user_id=bob_id,
            role_id="approver",
            granted_by_user_id=_user_id("user"),
        )
        # Sanity: with active grant, bob can view.
        r = bob_client.get(
            f"/api/forms/{open_form}/submissions/{handle}"
        )
        assert r.status_code == 200
        # Revoke the grant.
        assignments.revoke(
            assignment_id=row.id, revoked_by_user_id=_user_id("user"),
        )
        # NOTE: bob is still a granter? No — bob is the ASSIGNEE
        # here, not the granter. After revoke he's neither
        # assignee nor granter (the `granted_by_user_id` row
        # points at the regular user, not bob). So bob loses
        # visibility entirely.
        r = bob_client.get(
            f"/api/forms/{open_form}/submissions/{handle}"
        )
        assert r.status_code == 404, (
            f"revoked assignee still has visibility "
            f"(status={r.status_code}, body={r.text})"
        )

    def test_anonymous_visitor_denied_even_on_public_form(
        self,
        user_client: TestClient,
        anon_client: TestClient,
        admin_user, regular_user, open_form,
    ):
        # The form is public for filling — anyone reaches the form
        # URL. But viewing a submission requires being a contributor.
        # Anon visitors with no token must NOT see other users'
        # submissions just by guessing/knowing the handle.
        from frontflow.dsl import auth as _auth
        handle = _start_submission(user_client, open_form)
        _clear_cookies(anon_client)
        r = anon_client.get(
            f"/api/forms/{open_form}/submissions/{handle}"
        )
        assert r.status_code == 404

    def test_signed_link_token_bypasses_gate(
        self,
        user_client: TestClient,
        anon_client: TestClient,
        admin_user, regular_user, open_form,
    ):
        # A signed link explicitly grants a non-contributor access
        # to ONE submission. Anon visitor + valid token → 200.
        from frontflow.dsl import signed_links
        handle = _start_submission(user_client, open_form)
        bob_id = _user_id("user")
        token = signed_links.mint(
            user_id=bob_id, submission_handle=handle,
        )
        _clear_cookies(anon_client)
        r = anon_client.get(
            f"/api/forms/{open_form}/submissions/{handle}"
            f"?token={token}"
        )
        assert r.status_code == 200, (
            f"signed-link bearer was denied (status={r.status_code})"
        )

    def test_detail_endpoint_is_gated(
        self,
        user_client: TestClient,
        bob_client: TestClient,
        admin_user, regular_user, open_form,
    ):
        # /detail had NO check at all before Phase 7 — anyone with
        # a handle could see the full submission record. Verify
        # the gate fires here too.
        handle = _start_submission(user_client, open_form)
        r = bob_client.get(
            f"/api/forms/{open_form}/submissions/{handle}/detail"
        )
        assert r.status_code == 404, (
            f"non-contributor reached /detail "
            f"(status={r.status_code}, body={r.text})"
        )

    def test_steps_endpoint_is_gated(
        self,
        user_client: TestClient,
        bob_client: TestClient,
        admin_user, regular_user, open_form,
    ):
        # Verify the step read endpoint denies non-contributors.
        # Phase 4.5's role check on a public form would have
        # returned a 200 pending placeholder — but that leaks the
        # existence of the submission. Now: 404.
        handle = _start_submission(user_client, open_form)
        r = bob_client.get(
            f"/api/forms/{open_form}/submissions/{handle}"
            f"/steps/step1"
        )
        assert r.status_code == 404, (
            f"non-contributor reached /steps "
            f"(status={r.status_code}, body={r.text})"
        )


# ----------------------------------------------------------------------------
# Form-submissions listing endpoint visibility gate (Phase next-asks #5)
# ----------------------------------------------------------------------------

class TestFormSubmissionsListingGate:
    """`/api/forms/{form_id}/submissions` lists every submission of a
    form. Prior to this gate, a folder-grant user saw every row even
    though the per-submission detail endpoint scoped strictly. Now
    the listing applies the SAME submission-visibility rules as the
    detail endpoint — except that admins and folder-grant holders
    still see all rows (folder grants are form-owner grants).
    """

    @pytest.fixture
    def make_extra_user(self, anon_client: TestClient):
        """Factory: create a fresh non-admin user beyond the
        conftest's `regular_user`."""
        from frontflow.dsl import auth as _auth
        created: list[str] = []

        def _make(username: str) -> str:
            _auth.create_user(
                username, "pw-12345678", is_admin=False,
            )
            created.append(username)
            return username
        yield _make

    @pytest.fixture
    def bob_client(
        self, app, make_extra_user, anon_client: TestClient,
    ) -> TestClient:
        """An authenticated, non-admin, non-contributor user."""
        make_extra_user("bob")
        from fastapi.testclient import TestClient as _TC
        client = _TC(app, raise_server_exceptions=False)
        r = client.post(
            "/api/auth/login",
            json={"username": "bob", "password": "pw-12345678"},
        )
        assert r.status_code == 200
        yield client
        client.close()

    def test_anon_gets_401(
        self, anon_client: TestClient, admin_user, open_form,
    ):
        # The dep is now `_current_user` rather than the strict
        # folder-grant dep; an anonymous caller is still rejected.
        _clear_cookies(anon_client)
        r = anon_client.get(f"/api/forms/{open_form}/submissions")
        assert r.status_code == 401, (
            f"anon was admitted to the listing "
            f"(status={r.status_code}, body={r.text})"
        )

    def test_admin_sees_every_submission(
        self,
        admin_client: TestClient,
        user_client: TestClient,
        bob_client: TestClient,
        admin_user, regular_user, open_form,
    ):
        # Two different users start submissions; admin sees both.
        h1 = _start_submission(user_client, open_form)
        h2 = _start_submission(bob_client, open_form)
        # admin_client + user_client share a cookie jar (conftest
        # note) — user_client's login overwrote admin's. Re-login.
        admin_client.post("/api/auth/login", json=admin_user)
        r = admin_client.get(f"/api/forms/{open_form}/submissions")
        assert r.status_code == 200, r.text
        handles = {row["handle"] for row in r.json()}
        assert h1 in handles
        assert h2 in handles

    def test_non_contributor_sees_empty_listing(
        self,
        user_client: TestClient,
        bob_client: TestClient,
        admin_user, regular_user, open_form,
    ):
        # Regular user submits. Bob hits the listing — used to be
        # 403 by the folder-grant dep; now it's 200 with an empty
        # rows list because Bob has no per-submission visibility on
        # any row.
        _start_submission(user_client, open_form)
        r = bob_client.get(f"/api/forms/{open_form}/submissions")
        assert r.status_code == 200, r.text
        assert r.json() == []

    def test_step_submitter_sees_own_submission_only(
        self,
        user_client: TestClient,
        bob_client: TestClient,
        admin_user, regular_user, open_form,
    ):
        # Two submissions: one by regular user, one by Bob. Each
        # user's listing call must include their own and exclude
        # the other's. (This is the key regression-prevention test
        # — without the gate this would return BOTH rows for each.)
        own = _start_submission(user_client, open_form)
        others = _start_submission(bob_client, open_form)

        r_user = user_client.get(f"/api/forms/{open_form}/submissions")
        assert r_user.status_code == 200
        h_user = {row["handle"] for row in r_user.json()}
        assert own in h_user
        assert others not in h_user, (
            f"regular user saw Bob's submission in the listing: "
            f"{h_user}"
        )

        r_bob = bob_client.get(f"/api/forms/{open_form}/submissions")
        assert r_bob.status_code == 200
        h_bob = {row["handle"] for row in r_bob.json()}
        assert others in h_bob
        assert own not in h_bob, (
            f"Bob saw the regular user's submission in the listing: "
            f"{h_bob}"
        )

    def test_active_assignee_sees_assigned_submission(
        self,
        user_client: TestClient,
        bob_client: TestClient,
        admin_user, regular_user, open_form,
    ):
        # Regular user starts. Bob is granted an active assignment
        # on it. Bob's listing should now include that submission.
        handle = _start_submission(user_client, open_form)
        bob_id = _user_id("bob")
        assignments.grant(
            submission_handle=handle,
            user_id=bob_id,
            role_id="approver",
            granted_by_user_id=_user_id("user"),
        )
        r = bob_client.get(f"/api/forms/{open_form}/submissions")
        assert r.status_code == 200, r.text
        handles = {row["handle"] for row in r.json()}
        assert handle in handles, (
            f"active assignee was not shown the submission in the "
            f"listing (status={r.status_code}, handles={handles})"
        )

    def test_granter_sees_submission_they_granted_on(
        self,
        user_client: TestClient,
        bob_client: TestClient,
        admin_user, regular_user, open_form,
    ):
        # Bob, despite not being a contributor or assignee, granted
        # an assignment on this submission — the listing must
        # include it. Mirrors the granter-rule in
        # `can_view_submission`.
        handle = _start_submission(user_client, open_form)
        regular_id = _user_id("user")
        assignments.grant(
            submission_handle=handle,
            user_id=regular_id,
            role_id="approver",
            granted_by_user_id=_user_id("bob"),
        )
        r = bob_client.get(f"/api/forms/{open_form}/submissions")
        assert r.status_code == 200, r.text
        handles = {row["handle"] for row in r.json()}
        assert handle in handles, (
            f"granter was not shown the submission in the listing "
            f"(status={r.status_code}, handles={handles})"
        )

    def test_revoked_assignee_no_longer_sees_listing(
        self,
        user_client: TestClient,
        bob_client: TestClient,
        admin_user, regular_user, open_form,
    ):
        # Bob is granted then revoked. He should NOT see the
        # submission in the listing anymore (assignee path requires
        # active grant). Granter visibility is also not triggered
        # since the regular user, not Bob, did the grant.
        handle = _start_submission(user_client, open_form)
        bob_id = _user_id("bob")
        ann = assignments.grant(
            submission_handle=handle,
            user_id=bob_id,
            role_id="approver",
            granted_by_user_id=_user_id("user"),
        )
        assignments.revoke(
            assignment_id=ann.id,
            revoked_by_user_id=_user_id("admin"),
        )
        r = bob_client.get(f"/api/forms/{open_form}/submissions")
        assert r.status_code == 200, r.text
        handles = {row["handle"] for row in r.json()}
        assert handle not in handles, (
            f"revoked assignee still saw submission in the listing "
            f"(handles={handles})"
        )
