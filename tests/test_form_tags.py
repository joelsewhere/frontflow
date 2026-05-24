"""Tests for the @form(tags=...) feature.

Covers:
  - `@form(tags=[...])` is accepted; tags reach Workflow + CompiledWorkflow.
  - Forms without `tags=` get an empty list (default).
  - Tags surface on `GET /api/forms` per form, as `tags: [...]`.
  - Tags surface on the live FORMS registry (so the listing reads them
    without a DB join).
  - Tag order is preserved (declaration order, not sorted).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


EXAMPLES_DIR = (
    Path(__file__).parent.parent / "src" / "frontflow" / "examples"
)


@pytest.fixture
def production_examples(monkeypatch: pytest.MonkeyPatch):
    """Point WORKFLOW_SOURCE at the bundled examples for one test
    so the test can inspect each example's compiled metadata.
    Restores the fixture-form view on teardown so subsequent tests
    see a clean FORMS registry."""
    import frontflow.main as main_mod
    from frontflow.dsl.sources import workflow_source_from_uri

    original_source = main_mod.WORKFLOW_SOURCE
    monkeypatch.setattr(
        main_mod, "WORKFLOW_SOURCE",
        workflow_source_from_uri(str(EXAMPLES_DIR)),
    )
    main_mod.scan_workflows()
    yield main_mod
    monkeypatch.setattr(main_mod, "WORKFLOW_SOURCE", original_source)
    main_mod.scan_workflows()


class TestFormTagsDsl:
    def test_workflow_carries_tags(self, app):
        import frontflow.main as main_mod
        wf = main_mod.FORMS["test_tagged"]
        assert wf.tags == ["alpha", "beta", "gamma"]

    def test_untagged_form_has_empty_tags(self, app):
        # test_simple (a fixture form) declares no tags.
        import frontflow.main as main_mod
        wf = main_mod.FORMS["test_simple"]
        assert wf.tags == []

    def test_tags_order_preserved(self, app):
        # Declaration order matters — surfaces are display, not sets.
        import frontflow.main as main_mod
        wf = main_mod.FORMS["test_tagged"]
        # First entry is "alpha" not "beta" — sorted() would reorder.
        assert wf.tags[0] == "alpha"
        assert wf.tags[-1] == "gamma"


class TestFormTagsApi:
    def test_list_forms_includes_tags(self, admin_client: TestClient):
        r = admin_client.get("/api/forms")
        assert r.status_code == 200
        body = r.json()
        tagged = next(
            (f for f in body if f["form_id"] == "test_tagged"), None
        )
        assert tagged is not None, "tagged fixture form missing from list"
        assert tagged["tags"] == ["alpha", "beta", "gamma"]

    def test_untagged_form_list_entry_has_empty_list(
        self, admin_client: TestClient
    ):
        r = admin_client.get("/api/forms")
        body = r.json()
        simple = next(
            (f for f in body if f["form_id"] == "test_simple"), None
        )
        assert simple is not None
        assert simple["tags"] == []


class TestBundledExampleTags:
    """The bundled examples each got tags describing what they
    demonstrate. Verifies they survived a full compile + serve."""

    def test_each_bundled_example_has_at_least_one_tag(
        self, production_examples
    ):
        for form_id, wf in production_examples.FORMS.items():
            assert len(wf.tags) >= 1, (
                f"bundled example {form_id!r} should declare tags "
                "describing what it demonstrates"
            )

    def test_known_example_tags_match(self, production_examples):
        # A spot check — if these change, this test fails loud, which
        # is appropriate: the tags are part of the demo's pedagogy.
        expected = {
            "quickstart": {"quickstart", "single-page"},
            "notify_release": {"variables", "airflow", "templating"},
            "publish_article": {"airflow", "hitl", "showcase"},
        }
        for form_id, expected_tags in expected.items():
            wf = production_examples.FORMS.get(form_id)
            assert wf is not None, f"{form_id} missing from examples"
            assert set(wf.tags) == expected_tags, (
                f"{form_id}: tags drifted from expected set"
            )
