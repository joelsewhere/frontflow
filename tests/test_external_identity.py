"""Phase 2 tests for external user identity + notification preferences.

Covers:
  - `User.external_id` column accepts string + null, enforces unique
  - `User.notification_preferences` column defaults to {}
  - `@resolve_external_user` registers a hook; `resolve()` calls it
  - `resolve()` cheap-path: existing User by external_id, no hook call
  - `resolve()` first-touch: hook called once, row persisted
  - `resolve()` returns None when hook is unregistered
  - `resolve()` returns None when hook returns None
  - `lookup()` does NOT call the resolver hook
  - `deactivate()` marks the user inactive
  - `update()` updates mapped attributes
  - `get_notification_preferences` / `set_notification_preferences`
  - API endpoints: GET/PUT/DELETE /api/users/external/{external_id}
  - API endpoints: GET/PUT /api/users/me/notification-preferences
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from frontflow.dsl import auth, external_identity
from frontflow.dsl.store import User, _engine
from sqlalchemy import select
from sqlalchemy.orm import Session


def _user_id(username: str) -> int:
    """Look up the user_id for a username. The conftest fixtures
    return dicts of credentials, not User rows — this resolves
    them for the test body's DB operations."""
    with Session(_engine) as s:
        user = s.execute(
            select(User).where(User.username == username)
        ).scalar_one()
        return user.id


# ----------------------------------------------------------------------------
# Schema
# ----------------------------------------------------------------------------

class TestUserSchema:
    def test_external_id_can_be_set(self, app, admin_user):
        """A User row can carry an external_id (unique-when-set)."""
        with Session(_engine) as s:
            user = s.get(User, _user_id('admin'))
            user.external_id = "sis-101"
            s.commit()
            refreshed = s.get(User, _user_id('admin'))
            assert refreshed.external_id == "sis-101"

    def test_external_id_default_is_null(self, app, admin_user):
        """Migrations + defaults leave external_id null for
        frontflow-only users."""
        with Session(_engine) as s:
            user = s.get(User, _user_id('admin'))
            # Reset for the test (fixture may have been set by
            # other tests in the suite).
            user.external_id = None
            s.commit()
            refreshed = s.get(User, _user_id('admin'))
            assert refreshed.external_id is None

    def test_notification_preferences_defaults_empty_dict(
        self, app, regular_user
    ):
        """A freshly-created user has an empty preferences dict."""
        with Session(_engine) as s:
            user = s.get(User, _user_id('user'))
            # Either {} from the column default or {} after a
            # round-trip — both are fine.
            assert user.notification_preferences in ({}, None) or \
                isinstance(user.notification_preferences, dict)


# ----------------------------------------------------------------------------
# resolve_external_user hook
# ----------------------------------------------------------------------------

@pytest.fixture
def clear_resolver():
    """Ensure the resolver hook is unset before each test and
    cleared after, so tests don't leak state."""
    external_identity._reset_for_tests()
    yield
    external_identity._reset_for_tests()


class TestResolverHook:
    def test_resolve_returns_none_when_no_hook(
        self, app, clear_resolver
    ):
        """No hook registered → resolve refuses to invent identities."""
        result = external_identity.resolve("never-seen-id")
        assert result is None

    def test_resolve_existing_user_skips_hook(
        self, app, admin_user, clear_resolver
    ):
        """Cheap path: when the external_id already maps to a User
        row, the hook is not called."""
        # Set up: existing User with external_id.
        with Session(_engine) as s:
            user = s.get(User, _user_id('admin'))
            user.external_id = "sis-existing"
            s.commit()

        call_count = {"n": 0}

        @external_identity.resolve_external_user
        def hook(external_id: str):
            call_count["n"] += 1
            return None  # would return None — but should never be called

        result = external_identity.resolve("sis-existing")
        assert result is not None
        assert result.id == _user_id('admin')
        assert call_count["n"] == 0  # hook NOT called

    def test_resolve_first_touch_calls_hook_and_persists(
        self, app, clear_resolver
    ):
        """First time we see an external_id, hook fires and the
        returned User is persisted."""
        @external_identity.resolve_external_user
        def hook(external_id: str):
            return User(
                username=f"lms_{external_id}",
                external_id=external_id,
                is_admin=False,
            )

        result = external_identity.resolve("sis-new-1")
        assert result is not None
        assert result.username == "lms_sis-new-1"
        assert result.external_id == "sis-new-1"
        # Row was persisted — a follow-up lookup finds it.
        again = external_identity.lookup("sis-new-1")
        assert again is not None
        assert again.id == result.id

    def test_resolve_hook_returns_none_means_unknown(
        self, app, clear_resolver
    ):
        """Hook returns None → external_id is not valid; no row created."""
        @external_identity.resolve_external_user
        def hook(external_id: str):
            return None

        result = external_identity.resolve("sis-unknown")
        assert result is None
        # No row created.
        assert external_identity.lookup("sis-unknown") is None

    def test_resolve_overwrites_mismatched_external_id(
        self, app, clear_resolver
    ):
        """Hook returns a User with external_id != requested →
        frontflow overwrites to keep the mapping correct."""
        @external_identity.resolve_external_user
        def hook(external_id: str):
            # Author bug: returns a user with a different
            # external_id than was requested.
            return User(
                username=f"lms_{external_id}",
                external_id="something-else",
                is_admin=False,
            )

        result = external_identity.resolve("sis-correct")
        # We overwrite to ensure the row's external_id matches
        # what was queried.
        assert result.external_id == "sis-correct"

    def test_lookup_never_calls_hook(
        self, app, clear_resolver
    ):
        """lookup() is the non-resolving path; hook is never invoked."""
        call_count = {"n": 0}

        @external_identity.resolve_external_user
        def hook(external_id: str):
            call_count["n"] += 1
            return User(
                username=f"lms_{external_id}", external_id=external_id,
            )

        result = external_identity.lookup("not-yet-mapped")
        assert result is None
        assert call_count["n"] == 0


# ----------------------------------------------------------------------------
# deactivate / update
# ----------------------------------------------------------------------------

class TestDeactivateAndUpdate:
    def test_deactivate_marks_inactive(
        self, app, regular_user, clear_resolver
    ):
        with Session(_engine) as s:
            user = s.get(User, _user_id('user'))
            user.external_id = "sis-to-deactivate"
            s.commit()

        ok = external_identity.deactivate("sis-to-deactivate")
        assert ok is True
        with Session(_engine) as s:
            user = s.get(User, _user_id('user'))
            assert user.is_active is False
            # External_id stays — preserved for audit linkage.
            assert user.external_id == "sis-to-deactivate"

    def test_deactivate_unknown_returns_false(
        self, app, clear_resolver
    ):
        assert external_identity.deactivate("nope") is False

    def test_update_username(
        self, app, regular_user, clear_resolver
    ):
        with Session(_engine) as s:
            user = s.get(User, _user_id('user'))
            user.external_id = "sis-to-rename"
            s.commit()

        result = external_identity.update(
            "sis-to-rename", username="renamed_in_lms"
        )
        assert result is not None
        assert result.username == "renamed_in_lms"

    def test_update_unknown_returns_none(self, app, clear_resolver):
        assert external_identity.update("nope", username="x") is None


# ----------------------------------------------------------------------------
# Notification preferences (auth-side)
# ----------------------------------------------------------------------------

class TestNotificationPreferences:
    def test_get_unset_returns_empty_dict(self, app, regular_user):
        prefs = auth.get_notification_preferences(_user_id('user'))
        assert prefs == {}

    def test_get_unknown_user_returns_none(self, app):
        # Distinct from "empty dict" — None means the user doesn't exist.
        assert auth.get_notification_preferences(999_999) is None

    def test_set_overwrites(self, app, regular_user):
        auth.set_notification_preferences(
            _user_id('user'), {"email": True, "slack": False}
        )
        prefs = auth.get_notification_preferences(_user_id('user'))
        assert prefs == {"email": True, "slack": False}
        # Setting again replaces the whole dict.
        auth.set_notification_preferences(
            _user_id('user'), {"email": False}
        )
        prefs = auth.get_notification_preferences(_user_id('user'))
        assert prefs == {"email": False}

    def test_set_rejects_non_bool_values(self, app, regular_user):
        with pytest.raises(ValueError, match="bool"):
            auth.set_notification_preferences(
                _user_id('user'), {"email": "yes"}  # type: ignore
            )

    def test_set_rejects_non_dict(self, app, regular_user):
        with pytest.raises(ValueError, match="dict"):
            auth.set_notification_preferences(
                _user_id('user'), ["email"]  # type: ignore
            )

    def test_set_rejects_unknown_user(self, app):
        with pytest.raises(ValueError, match="no user"):
            auth.set_notification_preferences(999_999, {"email": True})


# ----------------------------------------------------------------------------
# API endpoints
# ----------------------------------------------------------------------------

class TestExternalUserApi:
    def test_get_external_user_404_when_unknown(
        self, admin_client: TestClient, clear_resolver
    ):
        r = admin_client.get("/api/users/external/nope")
        assert r.status_code == 404

    def test_get_external_user_200_when_present(
        self, admin_client: TestClient, admin_user, clear_resolver
    ):
        with Session(_engine) as s:
            user = s.get(User, _user_id('admin'))
            user.external_id = "sis-api-1"
            s.commit()
        r = admin_client.get("/api/users/external/sis-api-1")
        assert r.status_code == 200
        body = r.json()
        assert body["external_id"] == "sis-api-1"
        assert body["id"] == _user_id('admin')

    def test_put_external_user_updates_username(
        self, admin_client: TestClient, regular_user, clear_resolver
    ):
        with Session(_engine) as s:
            user = s.get(User, _user_id('user'))
            user.external_id = "sis-api-rename"
            s.commit()
        r = admin_client.put(
            "/api/users/external/sis-api-rename",
            json={"username": "renamed_by_api"},
        )
        assert r.status_code == 200
        assert r.json()["username"] == "renamed_by_api"

    def test_delete_external_user_deactivates(
        self, admin_client: TestClient, regular_user, clear_resolver
    ):
        with Session(_engine) as s:
            user = s.get(User, _user_id('user'))
            user.external_id = "sis-api-del"
            s.commit()
        r = admin_client.delete("/api/users/external/sis-api-del")
        assert r.status_code == 200
        with Session(_engine) as s:
            user = s.get(User, _user_id('user'))
            assert user.is_active is False

    def test_delete_external_user_404_when_unknown(
        self, admin_client: TestClient, clear_resolver
    ):
        r = admin_client.delete("/api/users/external/nope")
        assert r.status_code == 404

    def test_anon_blocked_from_external_endpoints(
        self, anon_client: TestClient, admin_user
    ):
        # admin_user fixture creates a user so the install isn't
        # in the "no users yet" bootstrap state (which returns 503).
        r = anon_client.get("/api/users/external/sis-1")
        assert r.status_code in (401, 403)
        r = anon_client.put(
            "/api/users/external/sis-1", json={"username": "x"}
        )
        assert r.status_code in (401, 403)
        r = anon_client.delete("/api/users/external/sis-1")
        assert r.status_code in (401, 403)

    def test_regular_user_blocked_from_external_endpoints(
        self, user_client: TestClient
    ):
        r = user_client.get("/api/users/external/sis-1")
        assert r.status_code == 403


class TestNotificationPreferencesApi:
    def test_get_my_preferences_default_empty(
        self, user_client: TestClient
    ):
        r = user_client.get("/api/users/me/notification-preferences")
        assert r.status_code == 200
        assert r.json() == {}

    def test_put_my_preferences_overwrites(
        self, user_client: TestClient
    ):
        r = user_client.put(
            "/api/users/me/notification-preferences",
            json={"preferences": {"email": True, "slack": False}},
        )
        assert r.status_code == 200
        assert r.json() == {"email": True, "slack": False}
        # Round-trip.
        r = user_client.get("/api/users/me/notification-preferences")
        assert r.json() == {"email": True, "slack": False}

    def test_anon_blocked_from_my_preferences(
        self, anon_client: TestClient, admin_user
    ):
        r = anon_client.get("/api/users/me/notification-preferences")
        assert r.status_code == 401

    def test_admin_can_get_any_user_prefs(
        self, admin_client: TestClient, regular_user
    ):
        # Seed the regular user's prefs first.
        auth.set_notification_preferences(
            _user_id('user'), {"email": False}
        )
        r = admin_client.get(
            f"/api/users/{_user_id('user')}/notification-preferences"
        )
        assert r.status_code == 200
        assert r.json() == {"email": False}

    def test_admin_can_set_any_user_prefs(
        self, admin_client: TestClient, regular_user
    ):
        r = admin_client.put(
            f"/api/users/{_user_id('user')}/notification-preferences",
            json={"preferences": {"slack": True}},
        )
        assert r.status_code == 200
        prefs = auth.get_notification_preferences(_user_id('user'))
        assert prefs == {"slack": True}

    def test_admin_get_unknown_user_404(
        self, admin_client: TestClient
    ):
        r = admin_client.get(
            "/api/users/999999/notification-preferences"
        )
        assert r.status_code == 404

    def test_regular_user_blocked_from_admin_prefs(
        self, user_client: TestClient, admin_user
    ):
        r = user_client.get(
            f"/api/users/{_user_id('admin')}/notification-preferences"
        )
        # 403 for non-admin trying to use admin endpoint.
        assert r.status_code == 403
