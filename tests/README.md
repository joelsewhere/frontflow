# frontflow tests

The backend test suite. Three layers:

1. **Unit tests** — pure functions, no I/O.
   `test_airflow_dispatch.py`, parts of `test_dsl_load.py`.

2. **Integration tests** — real FastAPI app via TestClient, real SQLite.
   `test_api_submissions.py`, `test_api_auth.py`.

3. **Airflow-mocked tests** — use the `fake_airflow_hook` fixture so
   no real Airflow cluster is needed.
   (Currently used by `test_airflow_dispatch.py` only; will expand
   when we add operator-level dispatch tests.)

Frontend tests deferred.

## Running

From the repo root — pytest config lives in `pyproject.toml`:

    pytest

Or via the build script (which gates on the suite):

    ./build.sh

Run a single file:

    pytest tests/test_airflow_dispatch.py

Run a single test:

    pytest tests/test_airflow_dispatch.py::TestDedupeClearOpsWithGraphs::test_subsumed_tasks_dropped

Random order is on by default via `pytest-randomly` — installed
with `pip install -e .[dev]`. To pin an order for debugging:

    pytest -p no:randomly

## CI

`.github/workflows/tests.yml` runs the suite on every push to main
and every PR, against Python 3.10 + 3.12. A red run blocks the PR.

## Fixtures

`conftest.py` provides:

- `app` — fresh FastAPI app, DB tables truncated, in-memory caches cleared.
  Every test that touches frontflow state should depend on this.
- `anon_client` — unauthenticated TestClient.
- `admin_client` — TestClient logged in as an admin user.
- `user_client` — TestClient logged in as a non-admin user.
- `admin_user` / `regular_user` — credential dicts; depend on these
  before `anon_client` to ensure at least one user exists (avoids the
  503 bootstrap response).
- `fake_airflow_hook` — replaces `_airflow_hook_for` with a stub that
  records calls. Pre-load `dag_tasks`, `dag_runs`, `task_instances`,
  `xcoms` before the SUT runs; assert on `.calls` after.

## Fixture forms

`tests/fixtures/forms/` holds minimal workflows that exercise
specific runtime behaviors:

- `test_simple.py` — single node, two text inputs. Validates the
  most basic create → submit round trip.
- `test_two_step.py` — two `@node`s chained with `>>`, second
  reads from first (cascade-aware). Validates chain advancement,
  edit cascade, and repin.

Add new fixture forms for new behaviors — keep them as small as
possible. Use the production examples (`src/frontflow/examples/`)
only via `test_dsl_load.py`, which scans them all for compile errors.

## Adding tests for a new feature

A new feature gets at minimum:

1. A unit test for any pure function the feature adds
   (e.g. `dedupe_clear_ops` lives in `test_airflow_dispatch.py`).
2. An integration test for the user-facing API path
   (e.g. a new endpoint gets a `test_api_*.py` test).
3. If the feature touches Airflow, a `fake_airflow_hook` test
   asserting on the exact calls made.

Regression tests: when fixing a bug, add a test that reproduces it
before the fix. Don't delete it after — that's the regression
guard.

## Known gaps

- No frontend tests (deferred per project decision).
- No end-to-end browser tests.
- No tests for: analytics endpoints, individual input types,
  preview mode, S3 storage, theme endpoints, access control.
  These are the next batch when the test coverage push continues.
