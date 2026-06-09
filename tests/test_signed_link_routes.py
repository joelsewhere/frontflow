"""Phase 5.5 tests — signed-link consumer endpoint.

Covers:
  - A signed-link-bearing visitor reaches a role-gated step
    (anonymous session, no cookie) and gets full access if the
    token authenticates them as an assigned user.
  - A bearer of an expired token gets pending state.
  - A bearer of a tampered/wrong-handle token gets pending state.
  - A token whose user's assignment was revoked falls through to
    anonymous — pending state.
  - The token-aware visibility check unblocks restricted forms
    for token-bearing visitors without a cookie session.
  - Cookie session takes precedence over token (cookie identity
    isn't overridden by a token for a different user).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from frontflow.dsl import assignments, signed_links
from frontflow.dsl.store import User, _engine


def _user_id(username: str) -> int:
    with Session(_engine) as s:
        return s.execute(
            select(User).where(User.username == username)
        ).scalar_one().id


def _clear_cookies(client: TestClient) -> None:
    """Clear the shared cookie jar — the conftest fixtures all
    use the same TestClient so an earlier admin login bleeds
    over. Anonymous tests must explicitly drop cookies."""
    client.cookies.clear()


def _start_submission(client: TestClient, form_id: str) -> str:
    r = client.post(
        f"/api/forms/{form_id}/submissions",
        json={"button": None, "values": {"prompt": "x"}},
    )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    return body.get("handle") or body["submission_id"]


# ----------------------------------------------------------------------------
# Anonymous signed-link visitor reaches role-gated step
# ----------------------------------------------------------------------------


class TestSignedLinkConsumer:
    def test_token_bearer_gets_full_access_when_assigned(
        self,
        admin_client: TestClient,
        anon_client: TestClient,
        admin_user, regular_user,
    ):
        # admin starts the submission, advances past landing.
        handle = _start_submission(admin_client, "auth_gated_form")
        # Grant the regular user the approver role.
        user_id = _user_id("user")
        admin_id = _user_id("admin")
        assignments.grant(
            submission_handle=handle,
            user_id=user_id,
            role_id="approver",
            granted_by_user_id=admin_id,
        )
        # Mint a token for that user.
        token = signed_links.mint(
            user_id=user_id, submission_handle=handle,
        )
        # ANON client (no cookie) hits the step with the token.
        _clear_cookies(anon_client)
        r = anon_client.get(
            f"/api/forms/auth_gated_form/submissions/{handle}/steps/step2"
            f"?token={token}"
        )
        assert r.status_code == 200, r.text
        access = r.json()["access"]
        assert access["can_read"] is True
        assert access["can_write"] is True
        assert access["pending"] is False

    def test_token_bearer_can_submit(
        self,
        admin_client: TestClient,
        anon_client: TestClient,
        admin_user, regular_user,
    ):
        handle = _start_submission(admin_client, "auth_gated_form")
        user_id = _user_id("user")
        admin_id = _user_id("admin")
        assignments.grant(
            submission_handle=handle,
            user_id=user_id,
            role_id="approver",
            granted_by_user_id=admin_id,
        )
        token = signed_links.mint(
            user_id=user_id, submission_handle=handle,
        )
        _clear_cookies(anon_client)
        r = anon_client.post(
            f"/api/forms/auth_gated_form/submissions/{handle}/steps/step2"
            f"?token={token}",
            json={"button": None, "values": {"decision": "Yes"}},
        )
        assert r.status_code == 200, r.text

    def test_revoked_assignment_token_falls_through_to_pending(
        self,
        admin_client: TestClient,
        anon_client: TestClient,
        admin_user, regular_user,
    ):
        handle = _start_submission(admin_client, "auth_gated_form")
        user_id = _user_id("user")
        admin_id = _user_id("admin")
        a = assignments.grant(
            submission_handle=handle,
            user_id=user_id,
            role_id="approver",
            granted_by_user_id=admin_id,
        )
        # Token minted while active.
        token = signed_links.mint(
            user_id=user_id, submission_handle=handle,
        )
        # Now revoke the assignment.
        assignments.revoke(
            assignment_id=a.id, revoked_by_user_id=admin_id,
        )
        # Token is still cryptographically valid, but the
        # assignment is gone. Should render pending.
        _clear_cookies(anon_client)
        r = anon_client.get(
            f"/api/forms/auth_gated_form/submissions/{handle}/steps/step2"
            f"?token={token}"
        )
        assert r.status_code == 200
        access = r.json()["access"]
        assert access["pending"] is True

    def test_tampered_token_falls_through_to_pending(
        self,
        admin_client: TestClient,
        anon_client: TestClient,
        admin_user, regular_user,
    ):
        handle = _start_submission(admin_client, "auth_gated_form")
        user_id = _user_id("user")
        admin_id = _user_id("admin")
        assignments.grant(
            submission_handle=handle,
            user_id=user_id,
            role_id="approver",
            granted_by_user_id=admin_id,
        )
        token = signed_links.mint(
            user_id=user_id, submission_handle=handle,
        )
        # Flip a character in the signature.
        payload, sig = token.split(".")
        bad_sig = ("A" + sig[1:]) if sig[0] != "A" else ("B" + sig[1:])
        bad_token = payload + "." + bad_sig
        _clear_cookies(anon_client)
        r = anon_client.get(
            f"/api/forms/auth_gated_form/submissions/{handle}/steps/step2"
            f"?token={bad_token}"
        )
        # Anon + tampered token: the submission-visibility gate
        # fires first. An anon user is neither admin nor folder-
        # granted nor a contributor nor a token-bearer (token
        # didn't verify), so they 404. Pre-Phase-7 this returned
        # 200 with a pending placeholder — but that leaked the
        # form/submission's existence to anyone with the URL.
        # The 404 is the correct, tighter behavior.
        assert r.status_code == 404

    def test_token_for_different_submission_falls_through(
        self,
        admin_client: TestClient,
        anon_client: TestClient,
        admin_user, regular_user,
    ):
        handle = _start_submission(admin_client, "auth_gated_form")
        user_id = _user_id("user")
        admin_id = _user_id("admin")
        assignments.grant(
            submission_handle=handle,
            user_id=user_id,
            role_id="approver",
            granted_by_user_id=admin_id,
        )
        # Mint a token bound to a DIFFERENT submission.
        token = signed_links.mint(
            user_id=user_id, submission_handle="other-handle",
        )
        _clear_cookies(anon_client)
        r = anon_client.get(
            f"/api/forms/auth_gated_form/submissions/{handle}/steps/step2"
            f"?token={token}"
        )
        # Same logic as the tampered-token test: anon visitor with
        # a token bound to a different submission isn't a contributor
        # on THIS submission. Submission gate denies → 404.
        assert r.status_code == 404

    def test_cookie_session_takes_precedence_over_token(
        self,
        admin_client: TestClient,
        user_client: TestClient,
        admin_user, regular_user,
    ):
        # admin_client has the admin's cookie; user_client has the
        # regular user's cookie. If we pass a token for the admin
        # to user_client, the cookie should win — user_client is
        # the regular user, not the admin.
        handle = _start_submission(admin_client, "auth_gated_form")
        admin_id = _user_id("admin")
        token = signed_links.mint(
            user_id=admin_id, submission_handle=handle,
        )
        r = user_client.get(
            f"/api/forms/auth_gated_form/submissions/{handle}/steps/step2"
            f"?token={token}"
        )
        # Regular user has no approver role → pending. Cookie
        # session wins; token is ignored.
        assert r.status_code == 200
        access = r.json()["access"]
        assert access["pending"] is True
        # Specifically: NOT permitted (otherwise the cookie was
        # bypassed and the token was honored — wrong).
        assert access["can_write"] is False
