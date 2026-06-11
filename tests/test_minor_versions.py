"""Tests for the minor-version system + configurable auto-repin.

Three behaviors covered:

  1. `upsert_form_version` bump classification:
     - same compiled hash, same source → no-op  (bump='none')
     - same compiled hash, different source → minor  (bump='minor', new row)
     - different compiled hash → major  (bump='major', new row, minor=0)

  2. `auto_repin_minor_submissions`:
     - migrates in-flight submissions on earlier minors
     - leaves terminal submissions alone
     - records a `submission_auto_repinned` event per migration

  3. `_should_auto_repin_minor` resolution priority:
     - form DSL setting beats env var
     - env var wins when DSL is silent
     - default (env unset) is False
"""
from __future__ import annotations

import frontflow.dsl.store as store
from frontflow.dsl.store import (
    Event,
    FormVersion,
    Session,
    Submission,
    _engine,
    auto_repin_minor_submissions,
    upsert_form_version,
)


# --- 1. upsert classification ---------------------------------------------


class TestUpsertBumpClassification:
    def test_first_upsert_is_major(self, app):  # app fixture seeds clean DB
        result = upsert_form_version(
            form_id="t_minor_first",
            name="t",
            folder_path="",
            compiled_graph={"id": "t_minor_first", "nodes": []},
            content_hash="abc",
            source="# v1",
        )
        assert result.bump == "major"
        assert result.version == 1
        assert result.minor_version == 0

    def test_identical_repeat_is_noop(self, app):
        r1 = upsert_form_version(
            form_id="t_minor_noop", name="t", folder_path="",
            compiled_graph={"id": "t_minor_noop"},
            content_hash="h1", source="# stable",
        )
        r2 = upsert_form_version(
            form_id="t_minor_noop", name="t", folder_path="",
            compiled_graph={"id": "t_minor_noop"},
            content_hash="h1", source="# stable",
        )
        assert r2.bump == "none"
        assert r2.form_version_id == r1.form_version_id
        assert r2.version == 1
        assert r2.minor_version == 0

    def test_source_only_change_bumps_minor(self, app):
        r1 = upsert_form_version(
            form_id="t_minor_bump", name="t", folder_path="",
            compiled_graph={"id": "t_minor_bump"},
            content_hash="h1", source="# original",
        )
        r2 = upsert_form_version(
            form_id="t_minor_bump", name="t", folder_path="",
            compiled_graph={"id": "t_minor_bump"},  # same compiled
            content_hash="h1",                       # same hash
            source="# edited helper",                # different source
        )
        assert r2.bump == "minor"
        assert r2.form_version_id != r1.form_version_id
        assert r2.version == 1
        assert r2.minor_version == 1

    def test_compiled_change_bumps_major_and_resets_minor(self, app):
        # Build up to v1.2, then make a structural change → v2.0.
        upsert_form_version(
            form_id="t_major", name="t", folder_path="",
            compiled_graph={"id": "t_major"},
            content_hash="h1", source="a",
        )
        upsert_form_version(
            form_id="t_major", name="t", folder_path="",
            compiled_graph={"id": "t_major"},
            content_hash="h1", source="b",
        )
        r_minor = upsert_form_version(
            form_id="t_major", name="t", folder_path="",
            compiled_graph={"id": "t_major"},
            content_hash="h1", source="c",
        )
        assert r_minor.version == 1 and r_minor.minor_version == 2

        r_major = upsert_form_version(
            form_id="t_major", name="t", folder_path="",
            compiled_graph={"id": "t_major", "added_node": True},
            content_hash="h2",
            source="c",
        )
        assert r_major.bump == "major"
        assert r_major.version == 2
        assert r_major.minor_version == 0


# --- 2. auto_repin_minor_submissions --------------------------------------


def _seed_submission(
    handle: str, form_version_id: int, state: str,
) -> None:
    """Insert a bare submission row pinned to a specific form_version."""
    from datetime import datetime, timezone
    with Session(_engine) as session:
        session.add(Submission(
            handle=handle,
            submission_id=handle,
            form_version_id=form_version_id,
            state=state,
            created_at=datetime.now(timezone.utc),
        ))
        session.commit()


class TestAutoRepin:
    def test_migrates_in_flight_only(self, app):
        # v1.0 baseline
        r1 = upsert_form_version(
            form_id="t_autorepin", name="t", folder_path="",
            compiled_graph={"id": "t_autorepin"},
            content_hash="h1", source="orig",
        )
        # Three submissions all pinned to v1.0, different states.
        _seed_submission("s_running", r1.form_version_id, "running")
        _seed_submission("s_pending", r1.form_version_id, "pending")
        _seed_submission("s_success", r1.form_version_id, "success")
        _seed_submission("s_failed", r1.form_version_id, "failed")

        # Source-only edit → v1.1
        r2 = upsert_form_version(
            form_id="t_autorepin", name="t", folder_path="",
            compiled_graph={"id": "t_autorepin"},
            content_hash="h1", source="edited",
        )
        assert r2.bump == "minor"

        migrated = auto_repin_minor_submissions(
            form_id="t_autorepin",
            major_version=1,
            new_form_version_id=r2.form_version_id,
        )
        # Only the two in-flight ones move.
        assert migrated == 2

        with Session(_engine) as session:
            running = session.get(Submission, "s_running")
            pending = session.get(Submission, "s_pending")
            success = session.get(Submission, "s_success")
            failed = session.get(Submission, "s_failed")
            assert running.form_version_id == r2.form_version_id
            assert pending.form_version_id == r2.form_version_id
            # Terminal submissions are NEVER touched.
            assert success.form_version_id == r1.form_version_id
            assert failed.form_version_id == r1.form_version_id

    def test_records_audit_event(self, app):
        r1 = upsert_form_version(
            form_id="t_evt", name="t", folder_path="",
            compiled_graph={"id": "t_evt"},
            content_hash="h1", source="orig",
        )
        _seed_submission("s_evt", r1.form_version_id, "running")
        r2 = upsert_form_version(
            form_id="t_evt", name="t", folder_path="",
            compiled_graph={"id": "t_evt"},
            content_hash="h1", source="edited",
        )
        auto_repin_minor_submissions(
            form_id="t_evt", major_version=1,
            new_form_version_id=r2.form_version_id,
        )
        with Session(_engine) as session:
            events = session.scalars(
                store.select(Event)
                .where(Event.submission_handle == "s_evt")
                .where(Event.type == "submission_auto_repinned")
            ).all()
            assert len(events) == 1
            e = events[0]
            assert e.payload["reason"] == "minor_bump"
            assert e.payload["form_id"] == "t_evt"
            assert e.payload["from_form_version_id"] == r1.form_version_id
            assert e.payload["to_form_version_id"] == r2.form_version_id
            # The event itself is tagged to the new form_version.
            assert e.form_version_id == r2.form_version_id

    def test_no_op_when_no_earlier_minors(self, app):
        r1 = upsert_form_version(
            form_id="t_noop_repin", name="t", folder_path="",
            compiled_graph={"id": "t_noop_repin"},
            content_hash="h1", source="orig",
        )
        # Don't bump — no earlier minors exist on this major.
        migrated = auto_repin_minor_submissions(
            form_id="t_noop_repin", major_version=1,
            new_form_version_id=r1.form_version_id,
        )
        assert migrated == 0


# --- 3. _should_auto_repin_minor resolution -------------------------------


class _FakeWorkflow:
    """Just enough surface area for the resolver — `auto_repin_minor`."""
    def __init__(self, setting):
        self.auto_repin_minor = setting


class TestAutoRepinResolution:
    def test_dsl_true_overrides_env_false(self, app, monkeypatch):
        # Force env default to False, then have the form opt in.
        monkeypatch.setattr(
            "frontflow.main.AUTO_REPIN_MINOR_ENV_DEFAULT", False,
        )
        from frontflow.main import _should_auto_repin_minor
        assert _should_auto_repin_minor(_FakeWorkflow(True)) is True

    def test_dsl_false_overrides_env_true(self, app, monkeypatch):
        monkeypatch.setattr(
            "frontflow.main.AUTO_REPIN_MINOR_ENV_DEFAULT", True,
        )
        from frontflow.main import _should_auto_repin_minor
        assert _should_auto_repin_minor(_FakeWorkflow(False)) is False

    def test_dsl_silent_falls_through_to_env(self, app, monkeypatch):
        monkeypatch.setattr(
            "frontflow.main.AUTO_REPIN_MINOR_ENV_DEFAULT", True,
        )
        from frontflow.main import _should_auto_repin_minor
        # None = silent → use env.
        assert _should_auto_repin_minor(_FakeWorkflow(None)) is True

        monkeypatch.setattr(
            "frontflow.main.AUTO_REPIN_MINOR_ENV_DEFAULT", False,
        )
        assert _should_auto_repin_minor(_FakeWorkflow(None)) is False

    def test_env_default_unset_is_false(self, app, monkeypatch):
        # Sanity: an env value of empty string resolves to False.
        monkeypatch.setenv("FRONTFLOW_AUTO_REPIN_MINOR", "")
        from frontflow.main import _read_auto_repin_env
        assert _read_auto_repin_env() is False

    def test_env_parses_truthy_strings(self, monkeypatch):
        from frontflow.main import _read_auto_repin_env
        for v in ("1", "true", "True", "YES", "on"):
            monkeypatch.setenv("FRONTFLOW_AUTO_REPIN_MINOR", v)
            assert _read_auto_repin_env() is True, f"failed for {v!r}"
        for v in ("0", "false", "no", "off", "", "anything-else"):
            monkeypatch.setenv("FRONTFLOW_AUTO_REPIN_MINOR", v)
            assert _read_auto_repin_env() is False, f"failed for {v!r}"
