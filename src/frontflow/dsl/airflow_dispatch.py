"""
Airflow operator dispatch — the real per-operator polling strategy.

This is what external.py's docstrings have been promising: the place
where an Airflow operator — TriggerDag, AirflowTaskSensor,
AirflowDagSensor, XComPull — actually acts on a live Airflow instance
through an AirflowHook, replacing the mock timer progression.

The runtime calls `advance_airflow_task` once per tick for each
connected Airflow external task. The function is idempotent: a task
already in a terminal state (success / failed) is returned untouched,
so a TriggerDag fires its POST exactly once and a sensor stops polling
the moment Airflow reports a terminal state.

The module is deliberately decoupled from runtime.py — it takes the
template resolver and the hook factory as callables — so it carries no
import dependency on the runtime and is unit-testable with fakes.

State shape. Every call returns a dict with at least `state`, one of
queued / running / success / failed — plus, for an AirflowHitl task
that is waiting on a person, `awaiting_response`. A TriggerDag also
carries `run_id` (the handle downstream sensors resolve against); an
XComPull carries `value` (the pulled XCom); an awaiting HITL task
carries `hitl` (the prompt the form renders); failures carry `detail`.

Ordering assumption. The runtime processes a node's external tasks in
chain order and only dispatches a task once the ones before it have
succeeded — so by the time a sensor runs, the TriggerDag's `run_id` is
already in the templating namespace and `resolve` can find it.
"""

from __future__ import annotations

from typing import Any, Callable

from .airflow_hook import AirflowError, AirflowHook
from .compile import CompiledExternalTask

# States from which there is nothing left to do.
_TERMINAL = {"success", "failed"}

# Airflow task-instance states → the chain's four-state alphabet.
# Anything unrecognized is treated as still running.
_TASK_STATE_MAP = {
    "success": "success",
    "skipped": "success",
    "failed": "failed",
    "upstream_failed": "failed",
    "removed": "failed",
    "running": "running",
    "up_for_retry": "running",
    "up_for_reschedule": "running",
    "restarting": "running",
    "deferred": "running",
    "queued": "queued",
    "scheduled": "queued",
    "none": "queued",
}

# Airflow DAG-run states — these line up with our alphabet directly.
_DAG_STATE_MAP = {
    "success": "success",
    "failed": "failed",
    "running": "running",
    "queued": "queued",
}


def _map_task_state(state: Any) -> str:
    if state is None:
        return "queued"
    return _TASK_STATE_MAP.get(str(state).lower(), "running")


def _map_dag_state(state: Any) -> str:
    if state is None:
        return "queued"
    return _DAG_STATE_MAP.get(str(state).lower(), "running")


def advance_airflow_task(
    task: CompiledExternalTask,
    prior_state: dict[str, Any] | None,
    *,
    resolve: Callable[[str], Any],
    get_hook: Callable[[str], AirflowHook],
) -> dict[str, Any]:
    """Advance one connected Airflow external task by a single tick and
    return its new state.

    `resolve` turns a `{{ steps.X.Y }}` template string into a value;
    `get_hook` turns a connection name into an AirflowHook. A task with
    no connection is returned unchanged — the caller decides those run
    on the mock instead.
    """
    prior = prior_state or {}
    if prior.get("state") in _TERMINAL:
        # Idempotent: never re-trigger, never re-poll a finished task.
        return prior

    connection = (task.config or {}).get("connection")
    if not connection:
        return prior

    try:
        hook = get_hook(connection)
        if task.kind == "airflow_trigger_dag":
            return _trigger(task, hook, resolve)
        if task.kind == "airflow_task_sensor":
            return _poll_task(task, hook, resolve)
        if task.kind == "airflow_dag_sensor":
            return _poll_dag(task, hook, resolve)
        if task.kind == "airflow_xcom_pull":
            return _pull_xcom(task, hook, resolve)
        if task.kind in ("airflow_hitl", "airflow_hitl_branch"):
            return _poll_hitl(task, hook, resolve)
    except AirflowError as e:
        return {"state": "failed", "detail": str(e)}

    # Unknown kind — leave the task as the caller found it.
    return prior


def _trigger(
    task: CompiledExternalTask,
    hook: AirflowHook,
    resolve: Callable[[str], Any],
) -> dict[str, Any]:
    """One-shot: POST the DAG run. The trigger is done the moment
    Airflow accepts it — the DAG actually running is a sensor's job."""
    cfg = task.config
    conf = {
        key: (resolve(value) if isinstance(value, str) else value)
        for key, value in (cfg.get("conf") or {}).items()
    }
    # An optional templated run id — when set, the DAG run is created
    # with exactly this id (it must be unique within the DAG).
    run_id_template = cfg.get("run_id_template")
    run_id_arg = (
        resolve(run_id_template) if run_id_template else None
    )
    run = hook.trigger_dag(cfg["dag_id"], conf=conf, run_id=run_id_arg)
    run_id = run.get("dag_run_id")
    return {
        "state": "success",
        "run_id": run_id,
        "detail": f"triggered run {run_id}",
    }


def _poll_task(
    task: CompiledExternalTask,
    hook: AirflowHook,
    resolve: Callable[[str], Any],
) -> dict[str, Any]:
    """Poll one Airflow task instance and map its state."""
    cfg = task.config
    run_id = resolve(cfg["run_id_template"])
    ti = hook.get_task_instance(cfg["dag_id"], run_id, cfg["task_id"])
    return {"state": _map_task_state(ti.get("state"))}


def _poll_dag(
    task: CompiledExternalTask,
    hook: AirflowHook,
    resolve: Callable[[str], Any],
) -> dict[str, Any]:
    """Poll a whole Airflow DAG run and map its state."""
    cfg = task.config
    run_id = resolve(cfg["run_id_template"])
    run = hook.get_dag_run(cfg["dag_id"], run_id)
    return {"state": _map_dag_state(run.get("state"))}


def _pull_xcom(
    task: CompiledExternalTask,
    hook: AirflowHook,
    resolve: Callable[[str], Any],
) -> dict[str, Any]:
    """One-shot: GET the XCom value a task pushed. The pulled value is
    surfaced downstream as `steps.<id>.value`."""
    cfg = task.config
    run_id = resolve(cfg["run_id_template"])
    value = hook.get_xcom(
        cfg["dag_id"], run_id, cfg["task_id"], cfg.get("key", "return_value")
    )
    return {"state": "success", "value": value}


def _poll_hitl(
    task: CompiledExternalTask,
    hook: AirflowHook,
    resolve: Callable[[str], Any],
) -> dict[str, Any]:
    """Poll a Human-in-the-loop task.

    Three outcomes. Before the DAG reaches the HITL task there is no
    detail yet — Airflow answers 404, which means "still running". Once
    the detail exists but no response has been recorded, the task is
    `awaiting_response` and carries the `hitl` prompt for the form to
    render. Once a response has been recorded, the task is `success`.
    """
    cfg = task.config
    run_id = resolve(cfg["run_id_template"])
    try:
        detail = hook.get_hitl_detail(cfg["dag_id"], run_id, cfg["task_id"])
    except AirflowError as e:
        # The HITL task isn't live yet — the DAG hasn't reached it.
        if e.status_code == 404:
            return {"state": "running"}
        raise

    # Airflow's hitlDetails response carries `response_received` once a
    # human has answered. Capture what they picked — `chosen_options`
    # and `params_input` — so the form can read it back as
    # `steps.<hitl id>.chosen_options` / `.params_input` and condition
    # on it. (This also picks up a response made in the Airflow UI.)
    if detail.get("response_received"):
        return {
            "state": "success",
            "chosen_options": detail.get("chosen_options") or [],
            "params_input": detail.get("params_input") or {},
        }

    # `params` is Airflow's param schema — each entry an object with
    # `value` / `description` / `schema`. Flatten to {name: default}
    # for the form; the schema's `type` rides along for field rendering.
    raw_params = detail.get("params") or {}
    params: dict[str, Any] = {}
    for name, spec in raw_params.items():
        if isinstance(spec, dict):
            params[name] = {
                "default": spec.get("value"),
                "type": (spec.get("schema") or {}).get("type", "string"),
                "description": spec.get("description"),
            }
        else:  # tolerate a bare value
            params[name] = {"default": spec, "type": "string",
                            "description": None}

    return {
        "state": "awaiting_response",
        "hitl": {
            "subject": detail.get("subject"),
            "body": detail.get("body"),
            "options": detail.get("options") or [],
            "params": params,
            "defaults": detail.get("defaults") or [],
            "multiple": bool(detail.get("multiple", False)),
        },
    }


def respond_to_hitl(
    task: CompiledExternalTask,
    *,
    resolve: Callable[[str], Any],
    get_hook: Callable[[str], AirflowHook],
    chosen_options: list[str],
    params_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Submit a person's response to an AirflowHitl task, resuming the
    DAG. Returns the task's new state — `success` on a clean PATCH.

    Raises AirflowError if the response can't be delivered, so the
    caller can let the person retry rather than failing the submission.
    """
    cfg = task.config
    connection = cfg.get("connection")
    if not connection:
        raise AirflowError("a HITL response needs a connected operator")
    hook = get_hook(connection)
    run_id = resolve(cfg["run_id_template"])
    hook.respond_hitl(
        cfg["dag_id"],
        run_id,
        cfg["task_id"],
        chosen_options=chosen_options,
        params_input=params_input or {},
    )
    # Record what was picked so the form can read it back as
    # `steps.<hitl id>.chosen_options` / `.params_input`.
    return {
        "state": "success",
        "detail": "response submitted",
        "chosen_options": chosen_options,
        "params_input": params_input or {},
    }
