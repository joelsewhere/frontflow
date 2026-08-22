"""Tests for `v_frontflow_submissions`, the flattened reporting view.

This is what a BI tool charts, so the properties worth protecting are
the ones a chart would silently get wrong:

  * soft-deleted submissions must not appear — every user-facing read
    path filters them, and a tombstoned submission showing up in a
    dashboard would be a leak nobody looked for;
  * `form_values` stays intact, so per-form fields remain reachable
    without a schema change when a form gains a field.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import text

from frontflow.dsl import store


def _rows() -> list[dict]:
    with store._engine.begin() as conn:
        result = conn.exec_driver_sql(
            f"SELECT * FROM {store.REPORTING_VIEW} ORDER BY step_seq"
        )
        return [dict(zip(result.keys(), row)) for row in result.fetchall()]


class TestViewShape:
    def test_view_exists_after_init(self, app):
        from sqlalchemy import inspect

        assert store.REPORTING_VIEW in inspect(store._engine).get_view_names()

    def test_recreating_is_idempotent(self, app):
        """init_db runs on every startup."""
        store.init_db()
        store.init_db()

        from sqlalchemy import inspect

        assert store.REPORTING_VIEW in inspect(store._engine).get_view_names()


class TestContent:
    def test_a_submission_appears_with_its_form_context(
        self, admin_client: TestClient
    ):
        """A chart needs form_id and the submitted values without
        knowing how frontflow normalises them."""
        r = admin_client.post(
            "/api/forms/test_simple/submissions",
            json={"values": {"name": "Ada", "note": "hello"}},
        )
        assert r.status_code == 201, r.text

        rows = _rows()
        assert rows, "the submission did not reach the reporting view"

        row = rows[0]
        assert row["form_id"] == "test_simple"
        assert row["form_name"]
        assert row["submission_state"] in ("running", "success")

        values = row["form_values"]
        if isinstance(values, str):  # SQLite stores JSON as text
            values = json.loads(values)
        assert values["name"] == "Ada"

    def test_soft_deleted_submissions_are_excluded(
        self, admin_client: TestClient
    ):
        """`deleted_at` is a tombstone, not a hard delete. Every
        user-facing read path filters it and the view must too."""
        r = admin_client.post(
            "/api/forms/test_simple/submissions",
            json={"values": {"name": "Ghost"}},
        )
        handle = r.json().get("handle")
        assert handle, r.text
        assert _rows(), "precondition: the row should be visible first"

        with store._engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE submission SET deleted_at = :ts WHERE handle = :h"
                ),
                {"ts": "2020-01-01 00:00:00", "h": handle},
            )

        remaining = [r_ for r_ in _rows() if r_["submission_handle"] == handle]
        assert not remaining, "a soft-deleted submission appeared in the view"
