"""Smoke tests for the example workflow files.

Every form in `src/frontflow/examples/*.py` should compile + register
without error. This catches the simplest regression class — a rename
on the DSL surface that breaks one of the examples, or a syntax
error introduced during refactor. Run on every commit.

Notes:
  - airflow_dags/*.py files are imported as part of the scan but
    they import `airflow` which isn't a test dependency. The scan
    reports them as load errors; the test asserts those are the
    ONLY load errors. If a non-airflow_dag file errors, the test
    fails.
  - This runs the production scanner against the production
    examples — not against the test fixtures.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


EXAMPLES_DIR = Path(__file__).parent.parent / "src" / "frontflow" / "examples"


@pytest.fixture
def production_examples(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Point WORKFLOW_SOURCE at the bundled examples for one test.

    `frontflow.main.WORKFLOW_SOURCE` is bound at module import — env
    monkeypatching alone isn't enough once the module is already
    loaded. Patch the attribute directly + force a rescan to restore
    fixture forms when the test ends so subsequent tests see a clean
    FORMS registry."""
    import frontflow.main as main_mod
    from frontflow.dsl.sources import workflow_source_from_uri

    original_source = main_mod.WORKFLOW_SOURCE
    monkeypatch.setattr(
        main_mod, "WORKFLOW_SOURCE",
        workflow_source_from_uri(str(EXAMPLES_DIR)),
    )
    main_mod.scan_workflows()
    yield main_mod
    # Restore the original source so the next test's `app` fixture
    # rescans against the fixture forms, not the production ones.
    monkeypatch.setattr(main_mod, "WORKFLOW_SOURCE", original_source)
    main_mod.scan_workflows()


class TestExampleFormsLoad:
    def test_every_example_compiles(self, production_examples):
        """Every .py in examples/ should compile + register. The
        only acceptable load errors are airflow_dags/* — those need
        the `airflow` package which isn't a test dependency."""
        forms_serving = set(production_examples.FORMS.keys())
        load_errors = production_examples.LOAD_ERRORS

        # Every expected example should be in the registry.
        expected = {
            "expense_reimbursement",
            "input_gallery",
            "multi_backend_pipeline",
            "notify_release",
            "onboarding",
            "publish_article",
            "quickstart",
            "speaker_submission",
        }
        missing = expected - forms_serving
        assert not missing, f"examples failed to register: {missing}"

        # Any load errors should ONLY be airflow_dags/*.
        unexpected_errors = {
            path: err
            for path, err in load_errors.items()
            if not path.startswith("airflow_dags/")
        }
        assert not unexpected_errors, (
            f"unexpected load errors: {unexpected_errors}"
        )

    def test_every_example_has_landing_step(self, production_examples):
        """Smoke check — each form's `landing_node` resolves to a
        real compiled node. Catches forms that compile but have no
        entry point."""
        for form_id, workflow in production_examples.FORMS.items():
            landing = workflow.landing_node()
            assert landing is not None, (
                f"{form_id}: landing_node() returned None"
            )
            assert landing.id, (
                f"{form_id}: landing node has no id"
            )
