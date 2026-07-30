"""Tests for `GET /forms/{form_id}/versions/{a}/diff/{b}` — the
version-comparison endpoint that powers the diff modal.

Three behaviors covered:
  1. Same-row comparison returns bump='none' with empty hunks.
  2. Source-only difference classifies as 'minor'; line counts and
     hunk shape are correct.
  3. Compiled-graph difference classifies as 'major'.
  4. Cross-form id rejection (404) and auth gating.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from frontflow.dsl.store import upsert_form_version


def _seed_version(
    form_id: str, source: str, content_hash: str,
    compiled_graph: dict | None = None,
) -> int:
    """Insert a form_version row and return its id. The caller controls
    `content_hash` to drive the bump classification — same hash across
    calls produces minor bumps; differing produces major."""
    result = upsert_form_version(
        form_id=form_id,
        name=form_id,
        folder_path="",
        compiled_graph=compiled_graph or {"id": form_id, "nodes": []},
        content_hash=content_hash,
        source=source,
    )
    return result.form_version_id


class TestDiffEndpoint:
    def test_same_version_returns_none_bump_and_empty_hunks(
        self, admin_client: TestClient,
    ):
        """A user picking the same version on both sides of the
        Compare modal should see "(identical)", not a 404."""
        vid = _seed_version("diff_same", "x = 1\n", "h1")
        r = admin_client.get(
            f"/api/forms/diff_same/versions/{vid}/diff/{vid}"
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["bump"] == "none"
        assert body["hunks"] == []
        assert body["added_lines"] == 0
        assert body["removed_lines"] == 0

    def test_minor_diff_classified_and_counted(
        self, admin_client: TestClient,
    ):
        """Source-only change (same content_hash) → bump='minor', with
        non-zero add/remove counts and a structured hunk."""
        v1 = _seed_version(
            "diff_minor", "x = 1\ny = 2\nz = 3\n", "h1",
        )
        v2 = _seed_version(
            "diff_minor", "x = 1\ny = 22\nz = 3\nw = 4\n", "h1",
        )
        assert v1 != v2  # sanity — minor bump created a new row
        r = admin_client.get(
            f"/api/forms/diff_minor/versions/{v1}/diff/{v2}"
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["bump"] == "minor"
        # `y = 2` removed, `y = 22` and `w = 4` added.
        assert body["removed_lines"] == 1
        assert body["added_lines"] == 2
        assert len(body["hunks"]) >= 1
        hunk = body["hunks"][0]
        kinds = [l["kind"] for l in hunk["lines"]]
        assert "remove" in kinds and "add" in kinds
        # Line numbers carried through: an "add" line has a to_lineno
        # but no from_lineno; a "remove" the other way around.
        for line in hunk["lines"]:
            if line["kind"] == "add":
                assert line["from_lineno"] is None
                assert line["to_lineno"] is not None
            elif line["kind"] == "remove":
                assert line["to_lineno"] is None
                assert line["from_lineno"] is not None
            else:
                assert line["from_lineno"] is not None
                assert line["to_lineno"] is not None

    def test_major_diff_classified(self, admin_client: TestClient):
        """Different compiled-graph hash → bump='major'."""
        v1 = _seed_version("diff_major", "src v1\n", "h1")
        v2 = _seed_version(
            "diff_major", "src v2\n", "h2",
            compiled_graph={"id": "diff_major", "nodes": ["new"]},
        )
        r = admin_client.get(
            f"/api/forms/diff_major/versions/{v1}/diff/{v2}"
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["bump"] == "major"

    def test_cross_form_ids_404(self, admin_client: TestClient):
        """An id for form A passed under form B's URL returns 404 —
        guards against probing other forms' sources via the URL."""
        v_a = _seed_version("diff_form_a", "src a\n", "ha")
        v_b = _seed_version("diff_form_b", "src b\n", "hb")
        r = admin_client.get(
            f"/api/forms/diff_form_a/versions/{v_a}/diff/{v_b}"
        )
        assert r.status_code == 404

    def test_nonexistent_id_404(self, admin_client: TestClient):
        v1 = _seed_version("diff_404", "src\n", "h1")
        r = admin_client.get(
            f"/api/forms/diff_404/versions/{v1}/diff/999999"
        )
        assert r.status_code == 404

    def test_requires_admin(
        self, anon_client: TestClient, user_client: TestClient,
    ):
        """Admin-only — both anon and non-admin are blocked. (The
        app's auth dependency returns 403 for unauthenticated and
        non-admin alike; the important thing is non-admins can't read
        cross-form source via this endpoint.)"""
        v1 = _seed_version("diff_auth", "src\n", "h1")
        v2 = _seed_version("diff_auth", "src2\n", "h1")
        r = anon_client.get(
            f"/api/forms/diff_auth/versions/{v1}/diff/{v2}"
        )
        assert r.status_code in (401, 403)
        r = user_client.get(
            f"/api/forms/diff_auth/versions/{v1}/diff/{v2}"
        )
        assert r.status_code == 403

    def test_reverse_direction_still_works(
        self, admin_client: TestClient,
    ):
        """Picking the NEWER version as the FROM side is legal — the
        UI should show the diff inverted (adds become removes, etc.).
        Useful when the admin wants to see "what would I lose if I
        rolled back?".
        """
        v1 = _seed_version("diff_rev", "old\n", "h1")
        v2 = _seed_version("diff_rev", "new\n", "h1")
        r = admin_client.get(
            f"/api/forms/diff_rev/versions/{v2}/diff/{v1}"
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["from_version"]["form_version_id"] == v2
        assert body["to_version"]["form_version_id"] == v1
        # "new" removed, "old" added — direction matters.
        kinds = [
            l["kind"] for h in body["hunks"] for l in h["lines"]
        ]
        assert "remove" in kinds and "add" in kinds
