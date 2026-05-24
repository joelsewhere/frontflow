"""Tests for the workflow graph endpoint — the structural DAG the
form summary page renders.

Focus: the new `pages` array (#18). The other graph fields (groups,
nodes, edges) are exercised by the full-stack examples — these tests
verify the page-container shape specifically:

  - Every `@page` in a workflow gets a GraphPage entry, always.
  - `member_group_ids` lists the node-group ids the page wraps,
    in declaration order.
  - A workflow with no pages emits an empty list, not null.
  - A page with a single section node still emits a page entry
    (no singleton suppression — the frontend decides rendering).

Fixture forms live at tests/fixtures/forms/test_*.py and are scanned
into FORMS by the `app` fixture's startup.
"""
from __future__ import annotations

from fastapi.testclient import TestClient


class TestPagesEmission:
    def test_multi_section_page_lists_members(
        self, admin_client: TestClient
    ):
        r = admin_client.get("/api/forms/test_multi_section/graph")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "pages" in body
        pages = body["pages"]
        assert len(pages) == 1
        intake = pages[0]
        assert intake["id"] == "intake"
        # Member ids in declaration order — basics first, then details.
        assert intake["member_group_ids"] == ["basics", "details"]

    def test_multi_section_page_title_defaults_to_id(
        self, admin_client: TestClient
    ):
        # No explicit title on @page → falls back to a humanized id
        # (the same `_humanize()` used for nodes — "intake" → "Intake",
        # "intake_step" → "Intake step").
        r = admin_client.get("/api/forms/test_multi_section/graph")
        page = r.json()["pages"][0]
        assert page["title"] == "Intake"

    def test_single_section_page_still_emits_entry(
        self, admin_client: TestClient
    ):
        # No singleton suppression — backend always emits, frontend
        # always draws (collapse is a frontend interaction).
        r = admin_client.get("/api/forms/test_single_section/graph")
        body = r.json()
        assert len(body["pages"]) == 1
        assert body["pages"][0]["id"] == "landing"
        assert body["pages"][0]["member_group_ids"] == ["basics"]

    def test_no_pages_workflow_emits_empty_list(
        self, admin_client: TestClient
    ):
        # The bundled test_two_step fixture is all @node, no @page.
        # Should emit pages=[] not pages=null.
        r = admin_client.get("/api/forms/test_two_step/graph")
        assert r.status_code == 200
        body = r.json()
        assert "pages" in body
        assert body["pages"] == []

    def test_flat_page_workflow_emits_synthetic_section(
        self, admin_client: TestClient
    ):
        # test_simple uses `@page` with the body returning a layout
        # directly (a "flat" page). The compiler synthesizes a single
        # implicit section node sharing the page's id, so the page's
        # `member_group_ids` is `[page_id]`.
        r = admin_client.get("/api/forms/test_simple/graph")
        assert r.status_code == 200
        body = r.json()
        assert len(body["pages"]) == 1
        page = body["pages"][0]
        assert page["id"] == "landing"
        assert page["member_group_ids"] == ["landing"]
