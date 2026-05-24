"""Shared pytest fixtures.

Strategy: env vars are set once at session-scope (FRONTFLOW_HOME,
DB_PATH, FRONTFLOW_SECRET_KEY, WORKFLOW_SOURCE) BEFORE any frontflow
module imports. Each test gets a fresh DB state via `truncate_all`
called on the SAME engine — that avoids the SQLAlchemy mapper-reload
hazard and is much faster than rebuilding the engine.

The `client` family is an authenticated FastAPI TestClient.
The `admin_client` is the same but logged in as an admin.
The `anon_client` is a bare client with no session cookie.

`fake_airflow_hook` patches `_airflow_hook_for` to return a stub that
records calls + returns canned responses, so tests for the Airflow
dispatch / clearing code don't need a live cluster.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Optional

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


FIXTURES_FORMS = Path(__file__).parent / "fixtures" / "forms"


# Set env vars before any frontflow import. Using a stable session-
# scoped tempdir; cleaned up at session end.
_SESSION_HOME = Path(tempfile.mkdtemp(prefix="frontflow-test-session-"))
os.environ["FRONTFLOW_HOME"] = str(_SESSION_HOME)
os.environ["DB_PATH"] = str(_SESSION_HOME / "db.sqlite")
os.environ["WORKFLOW_SOURCE"] = str(FIXTURES_FORMS)
os.environ["FRONTFLOW_SECRET_KEY"] = Fernet.generate_key().decode()


@pytest.fixture(scope="session", autouse=True)
def _cleanup_session_home():
    """Remove the session tempdir at the very end."""
    yield
    shutil.rmtree(_SESSION_HOME, ignore_errors=True)


@pytest.fixture
def app():
    """Returns the FastAPI app. Each test sees a clean DB state via
    table truncation — engine + mappers are stable across tests, only
    rows are wiped. Workflows are re-scanned on lifespan startup so
    FORMS / FORM_VERSION_IDS reflect the fixture forms."""
    import frontflow.dsl.store as store_mod
    import frontflow.main as main_mod

    # Ensure tables exist (idempotent).
    store_mod.init_db()
    # Wipe rows from every table so prior tests don't leak state.
    with store_mod._engine.begin() as conn:
        for table in reversed(store_mod.Base.metadata.sorted_tables):
            conn.execute(table.delete())
    # Wipe in-memory runtime caches — these live alongside the DB
    # and persist across tests if not explicitly cleared. A reused
    # submission_id would collide otherwise.
    import frontflow.dsl.runtime as runtime_mod
    with runtime_mod._submissions_lock:
        runtime_mod._submissions.clear()
        runtime_mod._id_index.clear()
    runtime_mod._preview_submissions.clear()
    # Scan workflows so FORMS / FORM_VERSION_IDS are populated for
    # tests that touch the runtime directly. The lifespan startup
    # ALSO does this when TestClient enters, but direct-runtime
    # tests bypass TestClient — scan here so both paths work.
    main_mod.scan_workflows()
    return main_mod.app


@pytest.fixture
def anon_client(app) -> Iterator[TestClient]:
    """Unauthenticated test client. Use for testing the auth gate
    (expect 401s) or the login flow itself."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_user(anon_client: TestClient) -> dict[str, str]:
    """Create + return credentials for an admin user. The user is
    created via the auth module directly, not the API, since there
    is no public create-admin endpoint."""
    import frontflow.dsl.auth as auth_mod

    username = "admin"
    password = "test-password-123"
    try:
        auth_mod.create_user(username, password, is_admin=True)
    except Exception:
        # Already exists from a prior fixture in the same test —
        # fine, the password is stable.
        pass
    return {"username": username, "password": password}


@pytest.fixture
def regular_user(anon_client: TestClient) -> dict[str, str]:
    """Create + return credentials for a non-admin user."""
    import frontflow.dsl.auth as auth_mod

    username = "user"
    password = "test-password-456"
    try:
        auth_mod.create_user(username, password, is_admin=False)
    except Exception:
        pass
    return {"username": username, "password": password}


@pytest.fixture
def admin_client(
    anon_client: TestClient, admin_user: dict[str, str]
) -> TestClient:
    """An authenticated admin TestClient — logged in via the API so
    the session cookie is real."""
    r = anon_client.post("/api/auth/login", json=admin_user)
    assert r.status_code == 200, f"admin login failed: {r.text}"
    return anon_client


@pytest.fixture
def user_client(
    anon_client: TestClient, regular_user: dict[str, str]
) -> TestClient:
    """An authenticated non-admin TestClient."""
    r = anon_client.post("/api/auth/login", json=regular_user)
    assert r.status_code == 200, f"user login failed: {r.text}"
    return anon_client


# ----------------------------------------------------------------- #
# Airflow fakes                                                     #
# ----------------------------------------------------------------- #


class FakeAirflowHook:
    """Records calls + returns canned responses. Replace the real
    AirflowHook in tests that exercise dispatch / clearing paths.

    Append your canned data to `.dag_runs`, `.task_instances`,
    `.dag_tasks` before invoking the SUT; assert on `.calls` after."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        # Canned responses, keyed for lookup.
        self.dag_runs: dict[tuple[str, str], dict[str, Any]] = {}
        self.task_instances: dict[
            tuple[str, str, str], dict[str, Any]
        ] = {}
        self.dag_tasks: dict[str, dict[str, list[str]]] = {}
        self.xcoms: dict[tuple[str, str, str, str], Any] = {}
        # If set, raise on the named method to simulate failure.
        self.raise_on: Optional[str] = None

    def _record(self, method: str, *args, **kwargs) -> None:
        self.calls.append((method, args, kwargs))
        if self.raise_on == method:
            raise RuntimeError(f"Simulated {method} failure")

    # ---- methods mirroring AirflowHook ----

    def trigger_dag(self, dag_id, *, conf=None, run_id=None):
        self._record("trigger_dag", dag_id, conf=conf, run_id=run_id)
        return {
            "dag_run_id": run_id or f"manual__{uuid.uuid4().hex[:8]}",
            "state": "queued",
        }

    def get_dag_run(self, dag_id, run_id):
        self._record("get_dag_run", dag_id, run_id)
        return self.dag_runs.get(
            (dag_id, run_id),
            {"dag_run_id": run_id, "state": "running"},
        )

    def get_task_instance(self, dag_id, run_id, task_id):
        self._record("get_task_instance", dag_id, run_id, task_id)
        return self.task_instances.get(
            (dag_id, run_id, task_id),
            {"task_id": task_id, "state": "running"},
        )

    def get_xcom(self, dag_id, run_id, task_id, key="return_value"):
        self._record("get_xcom", dag_id, run_id, task_id, key=key)
        return self.xcoms.get((dag_id, run_id, task_id, key))

    def get_dag_tasks(self, dag_id):
        self._record("get_dag_tasks", dag_id)
        return self.dag_tasks.get(dag_id, {})

    def clear_task_instances(
        self, dag_id, run_id, *, task_ids=None, include_downstream=True
    ):
        self._record(
            "clear_task_instances",
            dag_id,
            run_id,
            task_ids=task_ids,
            include_downstream=include_downstream,
        )
        return {"cleared": task_ids if task_ids else "all"}

    def get_hitl_detail(self, dag_id, run_id, task_id):
        self._record("get_hitl_detail", dag_id, run_id, task_id)
        return {"options": ["approve", "reject"], "params": {}}

    def respond_hitl(
        self, dag_id, run_id, task_id, *, chosen_options, params_input=None
    ):
        self._record(
            "respond_hitl",
            dag_id,
            run_id,
            task_id,
            chosen_options=chosen_options,
            params_input=params_input,
        )
        return {"ok": True}

    def close(self) -> None:
        self._record("close")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def fake_airflow_hook(monkeypatch: pytest.MonkeyPatch) -> FakeAirflowHook:
    """Patches `runtime._airflow_hook_for` to always return one
    FakeAirflowHook instance. Tests assert on `.calls` to verify
    what frontflow would have sent to Airflow."""
    import frontflow.dsl.runtime as rt_mod

    hook = FakeAirflowHook()
    monkeypatch.setattr(
        rt_mod, "_airflow_hook_for", lambda _connection=None: hook
    )
    return hook
