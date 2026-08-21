"""Share links must work end-to-end through the HTTP routes.

Regression: the unit tests for `signed_links` passed while the feature
was broken in production. Submission reads gated form visibility with
a token-BLIND `Depends(require_form_visibility)`, so on an unlisted or
restricted form a valid share link still 404'd with "form not found" —
the two gates (form visibility, submission visibility) both have to
accept the token, and only one did.

These tests exercise the composition through the real routes.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from frontflow.dsl import signed_links, store
from frontflow.dsl.store import Form, _engine


def _start_submission(client: TestClient, form_id: str) -> str:
    r = client.post(
        f"/api/forms/{form_id}/submissions",
        json={"button": None, "values": {"prompt": "x"}},
    )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    return body.get("handle") or body["submission_id"]


def _set_visibility(form_id: str, visibility: str) -> None:
    with Session(_engine) as db:
        row = db.get(Form, form_id)
        row.visibility = visibility
        if visibility == "unlisted" and not row.unlisted_token:
            row.unlisted_token = "unlisted-secret"
        db.commit()


@pytest.fixture
def shared(admin_client: TestClient, app):
    """A submission on the open fixture form, plus a share token."""
    form_id = "auth_open_form"
    handle = _start_submission(admin_client, form_id)
    token = signed_links.mint_for_share(submission_handle=handle)
    return form_id, handle, token


@pytest.mark.parametrize("visibility", ["public", "unlisted", "restricted"])
def test_share_link_reads_submission_at_any_form_visibility(
    admin_client: TestClient, shared, visibility,
):
    """The link is the credential — it must survive the form being
    unlisted or restricted, which is exactly what broke."""
    form_id, handle, token = shared
    _set_visibility(form_id, visibility)
    client = TestClient(admin_client.app)
    client.cookies.clear()  # anonymous

    r = client.get(
        f"/api/forms/{form_id}/submissions/{handle}", params={"token": token},
    )
    assert r.status_code == 200, f"{visibility}: {r.status_code} {r.text[:200]}"
    assert r.json()["form_id"] == form_id


def test_anonymous_without_token_is_refused(admin_client: TestClient, shared):
    form_id, handle, _token = shared
    _set_visibility(form_id, "unlisted")
    client = TestClient(admin_client.app)
    client.cookies.clear()
    r = client.get(f"/api/forms/{form_id}/submissions/{handle}")
    assert r.status_code == 404


def test_share_token_does_not_unlock_other_submissions(
    admin_client: TestClient, shared,
):
    form_id, _handle, token = shared
    other = _start_submission(admin_client, form_id)
    _set_visibility(form_id, "public")
    client = TestClient(admin_client.app)
    client.cookies.clear()
    r = client.get(
        f"/api/forms/{form_id}/submissions/{other}", params={"token": token},
    )
    assert r.status_code == 404


def test_share_token_cannot_write_a_comment(
    admin_client: TestClient, shared,
):
    """Read-only means read-only, even though the same token opens the
    submission for reading."""
    form_id, handle, token = shared
    _set_visibility(form_id, "public")
    client = TestClient(admin_client.app)
    client.cookies.clear()
    r = client.post(
        f"/api/forms/{form_id}/submissions/{handle}/comments/t1",
        params={"token": token},
        json={"body": "should not post"},
    )
    assert r.status_code == 404
