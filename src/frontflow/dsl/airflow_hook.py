"""
Airflow REST API client.

A thin HTTP client over an Airflow instance's public REST API — the
Airflow 3.x `/api/v2` surface. One AirflowHook is bound to one stored
connection (see the connection store in store.py) and turns its
credentials into authenticated requests.

This is the "hook" half of Airflow's hook/operator split: the workflow
operators (TriggerDag, the sensors, XComPull, Hitl — built in
later steps) call an AirflowHook; the hook owns authentication, URL
construction, and error translation, so the operators don't.

Authentication follows Airflow 3. A 'basic' connection exchanges its
username/password at `POST /auth/token` for a JWT bearer token; a
'token' connection already holds a bearer token and uses it directly.
The token is cached for the life of the hook instance.

Endpoint paths target Airflow 3.x. They're collected as f-strings in
one place per call so they're easy to adjust against a specific
deployment; the HITL response body in particular is pinned when the
HITL operator is built against a live instance.
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

import httpx


class AirflowError(RuntimeError):
    """Any failure reaching or authenticating with Airflow, or any error
    response it returns. Operators catch this and surface a failed step
    rather than letting the exception escape.

    `status_code` carries the HTTP status when the failure was an error
    response — so callers can distinguish, say, a 404 (the resource
    isn't there yet) from an outright failure."""

    def __init__(self, message: str, *, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class AirflowHook:
    """Authenticated client for one Airflow connection."""

    # A request that takes longer than this is treated as an unreachable
    # instance. Polling calls are short; triggering is short too.
    TIMEOUT = 15.0

    def __init__(
        self,
        connection: dict[str, Any],
        *,
        client: Optional[httpx.Client] = None,
    ) -> None:
        """`connection` is a record from store.get_connection — it must
        carry `base_url`, `auth_kind`, and the decrypted `secret`. A
        `client` may be injected for testing; otherwise a real one is
        created lazily."""
        if connection.get("conn_type") not in (None, "airflow"):
            raise AirflowError(
                f"connection {connection.get('name')!r} is not an Airflow "
                f"connection (type {connection.get('conn_type')!r})"
            )
        self._base_url = str(connection["base_url"]).rstrip("/")
        self._auth_kind = connection["auth_kind"]
        self._secret = connection["secret"]
        self._client = client or httpx.Client()
        self._owns_client = client is None
        self._cached_token: Optional[str] = None

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "AirflowHook":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- auth ---------------------------------------------------------------

    def _token(self) -> str:
        """The bearer token for API requests. For 'token' auth this is
        the stored token; for 'basic' auth it's a JWT obtained by
        exchanging the stored credentials at /auth/token."""
        if self._cached_token is not None:
            return self._cached_token

        if self._auth_kind == "token":
            token = self._secret.get("token")
            if not token:
                raise AirflowError("connection has no stored token")
            self._cached_token = token
            return token

        # basic — exchange username/password for a JWT.
        try:
            resp = self._client.post(
                f"{self._base_url}/auth/token",
                json={
                    "username": self._secret.get("username"),
                    "password": self._secret.get("password"),
                },
                timeout=self.TIMEOUT,
            )
        except httpx.RequestError as e:
            raise AirflowError(
                f"could not reach Airflow at {self._base_url}: {e}"
            ) from e
        if resp.status_code >= 400:
            raise AirflowError(
                f"Airflow authentication failed ({resp.status_code}) — "
                "check the connection's credentials"
            )
        token = resp.json().get("access_token")
        if not token:
            raise AirflowError("Airflow auth response carried no access_token")
        self._cached_token = token
        return token

    # -- transport ----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict[str, Any]] = None,
    ) -> Any:
        """Issue an authenticated request and return the parsed JSON
        body. Every failure mode is translated to AirflowError."""
        try:
            resp = self._client.request(
                method,
                f"{self._base_url}{path}",
                json=json,
                headers={"Authorization": f"Bearer {self._token()}"},
                timeout=self.TIMEOUT,
            )
        except httpx.RequestError as e:
            raise AirflowError(
                f"could not reach Airflow at {self._base_url}: {e}"
            ) from e

        if resp.status_code in (401, 403):
            raise AirflowError(
                f"Airflow rejected the request ({resp.status_code}) — "
                "the connection's credentials may be wrong or lack access",
                status_code=resp.status_code,
            )
        if resp.status_code == 404:
            raise AirflowError(
                f"Airflow returned 404 for {path}", status_code=404
            )
        if resp.status_code >= 400:
            raise AirflowError(
                f"Airflow error {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        if not resp.content:
            return {}
        return resp.json()

    # -- API surface --------------------------------------------------------

    def version(self) -> dict[str, Any]:
        """Airflow's version info. A cheap call for verifying that a
        connection is reachable and its credentials work."""
        return self._request("GET", "/api/v2/version")

    def trigger_dag(
        self,
        dag_id: str,
        *,
        conf: Optional[dict[str, Any]] = None,
        run_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Trigger a DAG run, optionally with a `conf` payload and an
        explicit `run_id`. Returns the created dag run — `dag_run_id` is
        the handle downstream pollers reference."""
        # Airflow 3's API requires `logical_date` in the trigger body.
        # A form-initiated run has no meaningful logical date, so it is
        # sent explicitly null — the API permits that.
        body: dict[str, Any] = {"conf": conf or {}, "logical_date": None}
        if run_id is not None:
            body["dag_run_id"] = run_id
        return self._request(
            "POST", f"/api/v2/dags/{quote(dag_id)}/dagRuns", json=body
        )

    def get_dag_run(self, dag_id: str, run_id: str) -> dict[str, Any]:
        """One DAG run — its `state` is queued / running / success /
        failed."""
        return self._request(
            "GET",
            f"/api/v2/dags/{quote(dag_id)}/dagRuns/{quote(run_id)}",
        )

    def get_dag_tasks(self, dag_id: str) -> dict[str, list[str]]:
        """Structural task list for a DAG. Returns a map
        `{task_id: [downstream_task_ids...]}` for every task in the
        DAG. Used by frontflow's clearing pipeline to dedupe clear
        ops whose Airflow-side downstream closure is already covered
        by another clear in the same batch.

        Airflow 2.x+ — `GET /api/v2/dags/{dag_id}/tasks`.
        """
        result = self._request(
            "GET", f"/api/v2/dags/{quote(dag_id)}/tasks"
        )
        graph: dict[str, list[str]] = {}
        for task in result.get("tasks") or []:
            task_id = task.get("task_id")
            if not task_id:
                continue
            downstream = task.get("downstream_task_ids") or []
            graph[task_id] = list(downstream)
        return graph

    def get_task_instance(
        self, dag_id: str, run_id: str, task_id: str
    ) -> dict[str, Any]:
        """One task instance within a DAG run — its `state` is the
        task's current execution state."""
        return self._request(
            "GET",
            f"/api/v2/dags/{quote(dag_id)}/dagRuns/{quote(run_id)}"
            f"/taskInstances/{quote(task_id)}",
        )

    def get_xcom(
        self,
        dag_id: str,
        run_id: str,
        task_id: str,
        key: str = "return_value",
    ) -> Any:
        """Pull one XCom value a task instance pushed. Defaults to the
        task's return value (`return_value`)."""
        entry = self._request(
            "GET",
            f"/api/v2/dags/{quote(dag_id)}/dagRuns/{quote(run_id)}"
            f"/taskInstances/{quote(task_id)}/xcomEntries/{quote(key)}",
        )
        return entry.get("value")

    def get_hitl_detail(
        self, dag_id: str, run_id: str, task_id: str, map_index: int = -1
    ) -> dict[str, Any]:
        """The Human-in-the-loop detail for a task instance — subject,
        body, options, params, and whether it's been responded to.
        Airflow 3.1+ only.

        The endpoint is nested under the task instance and carries the
        map index — `-1` for an unmapped (non-dynamic) task."""
        return self._request(
            "GET",
            f"/api/v2/dags/{quote(dag_id)}/dagRuns/{quote(run_id)}"
            f"/taskInstances/{quote(task_id)}/{map_index}/hitlDetails",
        )

    def respond_hitl(
        self,
        dag_id: str,
        run_id: str,
        task_id: str,
        *,
        chosen_options: list[str],
        params_input: Optional[dict[str, Any]] = None,
        map_index: int = -1,
    ) -> dict[str, Any]:
        """Submit a response to a Human-in-the-loop task, resuming the
        DAG. `chosen_options` is the picked option(s); `params_input`
        carries any form values. Airflow 3.1+ only.

        Same nested task-instance route as get_hitl_detail; the response
        body is `chosen_options` plus `params_input`."""
        return self._request(
            "PATCH",
            f"/api/v2/dags/{quote(dag_id)}/dagRuns/{quote(run_id)}"
            f"/taskInstances/{quote(task_id)}/{map_index}/hitlDetails",
            json={
                "chosen_options": chosen_options,
                "params_input": params_input or {},
            },
        )

    def clear_task_instances(
        self,
        dag_id: str,
        run_id: str,
        *,
        task_ids: Optional[list[str]] = None,
        include_downstream: bool = True,
    ) -> dict[str, Any]:
        """Clear task instances within one DAG run, so Airflow re-runs
        them. Used to realign Airflow with a frontflow edit/retry.

        `task_ids` is None to clear the *whole run* (every task
        instance), or a list to clear only those tasks. `clearTaskInstances`
        scopes to a single run via `dag_run_id` in the body.

        `include_downstream` (default True) mirrors Airflow's own
        "Clear" — clearing a task also clears its descendants, keeping
        the run internally consistent. `dry_run` is forced False: this
        is a real clear, not a preview.

        Airflow 2.x+ — `POST /api/v2/dags/{dag_id}/clearTaskInstances`.
        """
        body: dict[str, Any] = {
            "dag_run_id": run_id,
            "dry_run": False,
            "include_downstream": include_downstream,
            # Stay within this run — never fan out to other runs.
            "include_future": False,
            "include_past": False,
            "include_upstream": False,
        }
        if task_ids is not None:
            body["task_ids"] = task_ids
        return self._request(
            "POST",
            f"/api/v2/dags/{quote(dag_id)}/clearTaskInstances",
            json=body,
        )
