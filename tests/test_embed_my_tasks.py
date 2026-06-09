"""Phase 6 tests — /api/embed/my-tasks endpoint.

Covers:
  - 404 when FRONTFLOW_EMBED_ALLOWED_ORIGINS is unset
  - 401 without a token
  - 401 with an invalid/expired/wrong-issuer token
  - 200 with a valid embed token, returning the right user's tasks
  - CSP header: frame-ancestors honors the embed allowlist on
    /api/embed/* paths
  - CSP header: frame-ancestors 'none' when allowlist unset
  - assign_operator token is rejected (require_issuer enforced)
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from frontflow.dsl import assignments, signed_links
from frontflow.dsl.store import (
    Form, FormVersion, Submission as _SubRow, User, _engine,
    _utcnow as _now,
)


def _user_id(username: str) -> int:
    with Session(_engine) as s:
        return s.execute(
            select(User).where(User.username == username)
        ).scalar_one().id


def _clear_cookies(client: TestClient) -> None:
    client.cookies.clear()


@pytest.fixture
def embed_allowlist_set(monkeypatch):
    """Configure the install-wide embed allowlist for tests that
    expect the /embed surface to be live."""
    monkeypatch.setenv(
        "FRONTFLOW_EMBED_ALLOWED_ORIGINS",
        "https://portal.example.com",
    )
    yield
    # monkeypatch reverts on teardown.


@pytest.fixture
def embed_allowlist_unset(monkeypatch):
    """Explicitly clear the env var so 'not opted in' tests work
    even if a prior test set it."""
    monkeypatch.delenv(
        "FRONTFLOW_EMBED_ALLOWED_ORIGINS", raising=False,
    )
    yield


@pytest.fixture
def seeded_inbox(app, admin_user, regular_user):
    """Create a submission row + active assignment for the regular
    user so the inbox endpoint has something to return."""
    admin_id = _user_id("admin")
    user_id = _user_id("user")
    with Session(_engine) as s:
        s.merge(Form(
            form_id="embed_inbox_form",
            name="Embed inbox form",
            folder_path="",
        ))
        s.commit()
        ver = s.execute(
            select(FormVersion).where(
                FormVersion.form_id == "embed_inbox_form",
            )
        ).scalar_one_or_none()
        if ver is None:
            ver = FormVersion(
                form_id="embed_inbox_form",
                version=1,
                compiled_graph={"id": "embed_inbox_form", "title": "Embed inbox form"},
                content_hash="embed-1",
                source="",
                created_at=_now(),
            )
            s.add(ver)
            s.commit()
        s.add(_SubRow(
            handle="embed-inbox-handle",
            submission_id="embed-inbox-id",
            form_version_id=ver.id,
            state="running",
            created_at=_now(),
            updated_at=_now(),
        ))
        s.commit()

    assignments.grant(
        submission_handle="embed-inbox-handle",
        user_id=user_id,
        role_id="approver",
        granted_by_user_id=admin_id,
    )
    return {"user_id": user_id, "handle": "embed-inbox-handle"}


# ----------------------------------------------------------------------------
# /api/embed/my-tasks endpoint
# ----------------------------------------------------------------------------


class TestEmbedMyTasksEndpoint:
    def test_404_when_allowlist_unset(
        self, anon_client: TestClient, embed_allowlist_unset,
    ):
        # Not opted in → route 404s regardless of token.
        _clear_cookies(anon_client)
        r = anon_client.get("/api/embed/my-tasks?token=anything")
        assert r.status_code == 404

    def test_401_without_token(
        self, anon_client: TestClient, embed_allowlist_set,
    ):
        _clear_cookies(anon_client)
        r = anon_client.get("/api/embed/my-tasks")
        assert r.status_code == 401

    def test_401_with_invalid_token(
        self, anon_client: TestClient, embed_allowlist_set,
    ):
        _clear_cookies(anon_client)
        r = anon_client.get(
            "/api/embed/my-tasks?token=not-a-valid-token"
        )
        assert r.status_code == 401

    def test_401_with_assign_operator_token(
        self,
        anon_client: TestClient,
        embed_allowlist_set, seeded_inbox,
    ):
        """An assign_operator-issuer token should be rejected on
        the embed endpoint — require_issuer="embed" prevents
        confused-deputy use."""
        _clear_cookies(anon_client)
        # Mint an assign_operator-scope token (the wrong issuer).
        token = signed_links.mint(
            user_id=seeded_inbox["user_id"],
            submission_handle=seeded_inbox["handle"],
            scope="fill",
            issuer="assign_operator",
        )
        r = anon_client.get(f"/api/embed/my-tasks?token={token}")
        assert r.status_code == 401

    def test_200_with_valid_embed_token(
        self,
        anon_client: TestClient,
        embed_allowlist_set, seeded_inbox,
    ):
        _clear_cookies(anon_client)
        token = signed_links.mint_for_embed(
            user_id=seeded_inbox["user_id"],
        )
        r = anon_client.get(f"/api/embed/my-tasks?token={token}")
        assert r.status_code == 200, r.text
        body = r.json()
        handles = {row["submission_handle"] for row in body}
        assert seeded_inbox["handle"] in handles

    def test_returns_only_token_users_tasks(
        self,
        anon_client: TestClient,
        embed_allowlist_set,
        admin_user, regular_user,
    ):
        """A token for user A must not return user B's tasks."""
        admin_id = _user_id("admin")
        user_id = _user_id("user")
        # Seed two assignments — one for the admin, one for the user.
        with Session(_engine) as s:
            s.merge(Form(
                form_id="embed_iso_form", name="Iso form",
                folder_path="",
            ))
            s.commit()
            ver = s.execute(
                select(FormVersion).where(
                    FormVersion.form_id == "embed_iso_form",
                )
            ).scalar_one_or_none()
            if ver is None:
                ver = FormVersion(
                    form_id="embed_iso_form", version=1,
                    compiled_graph={"id": "embed_iso_form"},
                    content_hash="iso-1", source="",
                    created_at=_now(),
                )
                s.add(ver)
                s.commit()
            for h in ("iso-admin", "iso-user"):
                s.add(_SubRow(
                    handle=h, submission_id=h,
                    form_version_id=ver.id, state="running",
                    created_at=_now(), updated_at=_now(),
                ))
            s.commit()
        assignments.grant(
            submission_handle="iso-admin", user_id=admin_id,
            role_id="approver", granted_by_user_id=admin_id,
        )
        assignments.grant(
            submission_handle="iso-user", user_id=user_id,
            role_id="approver", granted_by_user_id=admin_id,
        )
        # Mint a token for the regular user — must NOT see the
        # admin's assignment.
        _clear_cookies(anon_client)
        token = signed_links.mint_for_embed(user_id=user_id)
        r = anon_client.get(f"/api/embed/my-tasks?token={token}")
        body = r.json()
        handles = {row["submission_handle"] for row in body}
        assert "iso-user" in handles
        assert "iso-admin" not in handles


# ----------------------------------------------------------------------------
# CSP header
# ----------------------------------------------------------------------------


class TestEmbedCSPHeader:
    def test_csp_honors_embed_allowlist(
        self, anon_client: TestClient, embed_allowlist_set,
    ):
        _clear_cookies(anon_client)
        # Make a request — even an unauthorized one carries the
        # CSP header from the middleware.
        r = anon_client.get("/api/embed/my-tasks")
        csp = r.headers.get("Content-Security-Policy", "")
        # Should NOT be 'none' — the allowlist permits the embed.
        assert "frame-ancestors" in csp
        assert "https://portal.example.com" in csp
        assert "'none'" not in csp

    def test_csp_none_when_allowlist_unset(
        self, anon_client: TestClient, embed_allowlist_unset,
    ):
        _clear_cookies(anon_client)
        r = anon_client.get("/api/embed/my-tasks?token=x")
        csp = r.headers.get("Content-Security-Policy", "")
        assert "frame-ancestors 'none'" in csp


# ----------------------------------------------------------------------------
# signed_links.mint_for_embed
# ----------------------------------------------------------------------------


class TestMintForEmbed:
    def test_produces_embed_scoped_token(self):
        token = signed_links.mint_for_embed(user_id=42)
        # Verify with the wildcard handle + embed issuer.
        payload = signed_links.verify(
            token, submission_handle="*", require_issuer="embed",
        )
        assert payload is not None
        assert payload["user_id"] == 42
        assert payload["issuer"] == "embed"
        assert payload["scope"] == "read"

    def test_embed_token_works_with_any_handle(self):
        """The wildcard binding means the token verifies for any
        submission_handle — that's the point. The role check
        downstream still narrows."""
        token = signed_links.mint_for_embed(user_id=42)
        payload = signed_links.verify(
            token, submission_handle="any-handle-at-all",
        )
        assert payload is not None
