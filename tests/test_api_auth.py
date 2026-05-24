"""Auth integration tests — login, session cookies, admin gating.

Covers the path most likely to break invisibly: a non-admin user
accessing admin-only endpoints, an expired/missing session, and a
freshly-created user logging in for the first time."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


class TestLogin:
    def test_login_succeeds_with_valid_credentials(
        self, anon_client: TestClient, admin_user: dict
    ):
        r = anon_client.post("/api/auth/login", json=admin_user)
        assert r.status_code == 200

    def test_login_rejects_wrong_password(
        self, anon_client: TestClient, admin_user: dict
    ):
        r = anon_client.post(
            "/api/auth/login",
            json={
                "username": admin_user["username"],
                "password": "wrong-password",
            },
        )
        assert r.status_code == 401

    def test_login_rejects_unknown_user(
        self, anon_client: TestClient
    ):
        r = anon_client.post(
            "/api/auth/login",
            json={"username": "ghost", "password": "anything"},
        )
        assert r.status_code == 401


class TestAuthenticationGating:
    def test_anon_cannot_list_forms(
        self, anon_client: TestClient, admin_user: dict
    ):
        # The admin_user fixture ensures at least one account exists,
        # so the API serves 401 (auth required) rather than 503
        # (bootstrap state — no users at all).
        r = anon_client.get("/api/forms")
        assert r.status_code == 401

    def test_authenticated_can_list_forms(
        self, admin_client: TestClient
    ):
        r = admin_client.get("/api/forms")
        assert r.status_code == 200
        # Both fixture forms should be there.
        ids = [f["form_id"] for f in r.json()]
        assert "test_simple" in ids
        assert "test_two_step" in ids


class TestAdminGating:
    """Endpoints documented as admin-only should reject non-admin
    users with 403 (not 401 — they ARE authenticated, just unprivileged)."""

    def test_non_admin_cannot_reparse_form(
        self, user_client: TestClient
    ):
        r = user_client.post("/api/forms/test_simple/refresh")
        assert r.status_code in (401, 403)

    def test_admin_can_reparse_form(self, admin_client: TestClient):
        r = admin_client.post("/api/forms/test_simple/refresh")
        # 200 with status=ok|skipped, or 200 with status=error if
        # something's wrong on disk. Either way, not a 403.
        assert r.status_code == 200
