"""Tests for the `@form(private=True)` flag.

Principle: DSL is the source of truth for visibility. When a form
declares `private=True`, the admin UI cannot override that — the
author edits the source (and commits) to change it.

Covers:
  - First-time form discovery with `private=True` sets visibility
    to "restricted".
  - Re-scanning enforces DSL-declared visibility on EVERY scan
    (the DSL is the source of truth, not just an initial state).
  - `set_form_visibility` raises FormVisibilityLocked on a
    DSL-locked form; the API endpoint returns 409.
  - `get_form_visibility` reports `dsl_locked: True` for DSL-locked
    forms so the admin UI can disable the controls.
  - Forms without DSL-declared visibility still let admins manage
    visibility freely (the existing behavior, unchanged).
  - The default (no `private=`) creates forms as `public`.
  - `upsert_form_version` rejects unknown dsl_visibility values.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from frontflow.dsl import store
from frontflow.dsl.auth import (
    FormVisibilityLocked,
    get_form_visibility,
    set_form_visibility,
)


class TestInitialVisibility:
    def test_private_form_starts_restricted(self, app):
        info = get_form_visibility("test_private")
        assert info is not None
        assert info["visibility"] == "restricted"

    def test_non_private_form_starts_public(self, app):
        info = get_form_visibility("test_simple")
        assert info is not None
        assert info["visibility"] == "public"


class TestDslIsSourceOfTruth:
    def test_admin_cannot_change_dsl_locked_form(self, app):
        """The DSL declares this form private; the admin UI must
        not be able to flip it. Edit the source instead."""
        with pytest.raises(FormVisibilityLocked) as exc:
            set_form_visibility("test_private", "public")
        # Error message points the user at the right action.
        assert "DSL" in str(exc.value) or "source" in str(exc.value)

    def test_dsl_lock_reported_in_get(self, app):
        info = get_form_visibility("test_private")
        assert info is not None
        assert info["dsl_locked"] is True

    def test_unlocked_forms_report_dsl_locked_false(self, app):
        info = get_form_visibility("test_simple")
        assert info is not None
        assert info["dsl_locked"] is False

    def test_dsl_visibility_re_enforced_on_rescan(self, app):
        """If the DB ends up out of sync — e.g. data migrated from
        a previous schema, or the row was tampered with directly —
        the next scan snaps the visibility back to what the DSL
        declares."""
        import frontflow.main as main_mod

        # Direct DB poke — bypass the locked setter to simulate drift.
        from frontflow.dsl.store import _engine, Form
        from sqlalchemy.orm import Session
        with Session(_engine) as session:
            form = session.get(Form, "test_private")
            assert form is not None
            form.visibility = "public"  # drift
            session.commit()

        # Re-scan: DSL is enforced; visibility snaps back.
        main_mod.scan_workflows()
        info = get_form_visibility("test_private")
        assert info is not None
        assert info["visibility"] == "restricted"


class TestAdminControlWhenDslSilent:
    """Forms whose DSL doesn't declare visibility behave the same as
    before this flag — admin owns the value, rescans don't trample
    it. Confirms the fix doesn't regress that existing flow."""

    def test_admin_can_change_unlocked_form(self, app):
        set_form_visibility("test_simple", "restricted")
        info = get_form_visibility("test_simple")
        assert info is not None
        assert info["visibility"] == "restricted"
        # Restore.
        set_form_visibility("test_simple", "public")

    def test_admin_change_survives_rescan_when_dsl_silent(self, app):
        import frontflow.main as main_mod
        set_form_visibility("test_simple", "restricted")
        main_mod.scan_workflows()
        info = get_form_visibility("test_simple")
        assert info is not None
        assert info["visibility"] == "restricted"
        # Restore.
        set_form_visibility("test_simple", "public")


class TestApiSurface:
    def test_anon_blocked_from_private_form(
        self, anon_client: TestClient
    ):
        r = anon_client.get("/api/forms/test_private")
        assert r.status_code == 404

    def test_admin_can_reach_private_form(
        self, admin_client: TestClient
    ):
        r = admin_client.get("/api/forms/test_private")
        assert r.status_code == 200

    def test_visibility_change_api_returns_409_when_dsl_locked(
        self, admin_client: TestClient
    ):
        r = admin_client.put(
            "/api/forms/test_private/visibility",
            json={"visibility": "public"},
        )
        assert r.status_code == 409
        assert "DSL" in r.json()["detail"] or "source" in r.json()["detail"]


class TestStoreValidation:
    def test_invalid_dsl_visibility_raises(self):
        with pytest.raises(ValueError, match="unknown dsl_visibility"):
            store.upsert_form_version(
                form_id="t_bad_vis",
                name="t",
                folder_path="",
                compiled_graph={"id": "t_bad_vis"},
                content_hash="x",
                source="",
                dsl_visibility="totally_not_a_real_value",
            )
