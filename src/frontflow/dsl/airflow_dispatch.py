"""
Airflow operator dispatch — the real per-operator polling strategy.

This is what external.py's docstrings have been promising: the place
where an Airflow operator — TriggerDag, TaskSensor,
DagSensor, XComPull — actually acts on a live Airflow instance
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
queued / running / success / failed — plus, for an Hitl task
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
    node_id: str = "",
    cleared_run_ids: dict[str, str] | None = None,
    form_values: dict[str, Any] | None = None,
    steps_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Advance one connected Airflow external task by a single tick and
    return its new state.

    `resolve` turns a `{{ steps.X.Y }}` template string into a value;
    `get_hook` turns a connection name into an AirflowHook. A task with
    no connection is returned unchanged — the caller decides those run
    on the mock instead.

    `cleared_run_ids` is the submission's stash of pre-clear run ids
    (keyed `node_id::task_id`); when a `trigger_dag` with an explicit
    run id finds its run id unchanged there, it re-attaches to the
    cleared run instead of triggering a new one.

    `form_values` is the submitting step's form-values dict — used by
    `airflow_hitl_response` when `params_input` is None to default to
    the form's submitted values. Other operator kinds ignore it.
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
            return _trigger(
                task, hook, resolve,
                node_id=node_id,
                cleared_run_ids=cleared_run_ids or {},
            )
        if task.kind == "airflow_task_sensor":
            return _poll_task(task, hook, resolve)
        if task.kind == "airflow_task_state_sensor":
            return _poll_task_state(task, hook, resolve)
        if task.kind == "airflow_dag_sensor":
            return _poll_dag(task, hook, resolve)
        if task.kind == "airflow_xcom_pull":
            return _pull_xcom(task, hook, resolve)
        if task.kind in ("airflow_hitl", "airflow_hitl_branch"):
            return _poll_hitl(task, hook, resolve)
        if task.kind == "airflow_hitl_response":
            return _send_hitl_response(
                task, hook, resolve, form_values or {},
                steps_data=steps_data or {},
            )
    except AirflowError as e:
        return {"state": "failed", "detail": str(e)}

    # Unknown kind — leave the task as the caller found it.
    return prior


# Operators that, when affected by a frontflow edit, need an Airflow
# clear. A `trigger_dag` owns the run it created; a sensor / xcom /
# hitl operator points at one task instance. A `dag_sensor` only
# *observes* a run it does not own, so it clears nothing in Airflow.
_OWNS_RUN = {"airflow_trigger_dag"}
_OWNS_TASK = {
    "airflow_task_sensor",
    "airflow_task_state_sensor",
    "airflow_xcom_pull",
    "airflow_hitl",
    "airflow_hitl_branch",
    "airflow_hitl_response",
}


def plan_airflow_clear(
    task: CompiledExternalTask,
    state: dict[str, Any] | None,
    *,
    resolve: Callable[[str], Any],
) -> dict[str, Any] | None:
    """Work out what clearing this affected operator means in Airflow.

    Returns a clear-op descriptor — `{dag_id, run_id, task_ids}` — or
    None when there is nothing to clear (a `dag_sensor`, or an operator
    that never reached a run). `task_ids` is None for a whole-run
    clear (an affected `trigger_dag`) or a one-element list for a
    single task instance (an affected sensor / xcom / hitl).

    This only *plans* the clear; the caller collects every plan across
    the affected steps, dedupes them, and then issues the calls — so a
    task clear subsumed by a whole-run clear of the same run can be
    dropped before any network traffic.
    """
    cfg = task.config or {}
    dag_id = cfg.get("dag_id")
    if not dag_id:
        return None

    if task.kind in _OWNS_RUN:
        # The run id was recorded in state when the DAG was triggered.
        run_id = (state or {}).get("run_id")
        if not run_id:
            # Never triggered — nothing exists in Airflow yet.
            return None
        return {"dag_id": dag_id, "run_id": run_id, "task_ids": None}

    if task.kind in _OWNS_TASK:
        run_id_template = cfg.get("run_id_template")
        task_id = cfg.get("task_id")
        if not run_id_template or not task_id:
            return None
        try:
            run_id = resolve(run_id_template)
        except Exception:  # noqa: BLE001 - resolution best-effort
            return None
        if not run_id:
            return None
        return {
            "dag_id": dag_id,
            "run_id": str(run_id),
            "task_ids": [task_id],
        }

    # dag_sensor and anything else: observes, does not own — no clear.
    return None


def _downstream_closure(
    task_id: str, graph: dict[str, list[str]]
) -> set[str]:
    """Set of every task reachable from `task_id` via the DAG's
    edges, excluding `task_id` itself. Cycles are defensively guarded
    against — Airflow DAGs are acyclic by definition, but a malformed
    graph response shouldn't infinite-loop us."""
    closure: set[str] = set()
    stack: list[str] = list(graph.get(task_id, []))
    while stack:
        nxt = stack.pop()
        if nxt in closure:
            continue
        closure.add(nxt)
        stack.extend(graph.get(nxt, []))
    return closure


def _drop_subsumed_task_ids(
    task_ids: set[str], graph: dict[str, list[str]]
) -> set[str]:
    """Drop any task id whose downstream closure (within `graph`)
    already contains another task id in `task_ids`. Clearing the
    ancestor with `include_downstream=True` covers the descendant.

    Implementation: compute each task's downstream closure once, then
    drop any descendant that appears in some other task's closure. If
    two task ids are mutually unreachable from each other, both stay.
    """
    closures = {tid: _downstream_closure(tid, graph) for tid in task_ids}
    subsumed: set[str] = set()
    for tid, closure in closures.items():
        for other in task_ids:
            if other == tid or other in subsumed:
                continue
            if other in closure:
                subsumed.add(other)
    return task_ids - subsumed


def dedupe_clear_ops(
    ops: list[dict[str, Any]],
    *,
    dag_graphs: dict[str, dict[str, list[str]]] | None = None,
) -> list[dict[str, Any]]:
    """Collapse a list of clear-op descriptors so nothing is cleared
    redundantly at the granularity frontflow can see.

    A whole-run clear (`task_ids` None) for a `(dag_id, run_id)`
    subsumes any task-instance clear of the same run — those are
    dropped. Remaining task-instance clears of the same run are merged
    into one op with the union of their task ids.

    `dag_graphs` (optional) is a map `{dag_id: {task_id: [downstream...]}}`.
    When provided, task ids whose Airflow-side downstream closure is
    already covered by another task id in the same `(dag_id, run_id)`
    batch are dropped — `clear_task_instances` is called with
    `include_downstream=True`, so clearing the upstream covers the
    downstream redundantly. Without `dag_graphs`, this dedupe is
    skipped (the redundant calls still work, just less efficiently).
    """
    whole_run: set[tuple[str, str]] = set()
    task_clears: dict[tuple[str, str], set[str]] = {}
    for op in ops:
        key = (op["dag_id"], op["run_id"])
        if op["task_ids"] is None:
            whole_run.add(key)
        else:
            task_clears.setdefault(key, set()).update(op["task_ids"])

    if dag_graphs:
        for (dag_id, _run_id), task_ids in task_clears.items():
            graph = dag_graphs.get(dag_id)
            if not graph or len(task_ids) < 2:
                continue
            task_clears[(dag_id, _run_id)] = _drop_subsumed_task_ids(
                task_ids, graph
            )

    result: list[dict[str, Any]] = [
        {"dag_id": d, "run_id": r, "task_ids": None}
        for (d, r) in sorted(whole_run)
    ]
    for (d, r), task_ids in sorted(task_clears.items()):
        if (d, r) in whole_run:
            continue  # subsumed by the whole-run clear
        result.append(
            {
                "dag_id": d,
                "run_id": r,
                "task_ids": sorted(task_ids),
            }
        )
    return result


def _trigger(
    task: CompiledExternalTask,
    hook: AirflowHook,
    resolve: Callable[[str], Any],
    *,
    node_id: str = "",
    cleared_run_ids: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One-shot: POST the DAG run. The trigger is done the moment
    Airflow accepts it — the DAG actually running is a sensor's job.

    Re-attach: if a prior edit cleared this trigger's run and the run
    was created with an *explicit* run id that still resolves to the
    same value, skip the POST and re-attach to the cleared run — it is
    being re-run in place by Airflow. A trigger with no explicit run
    id, or whose run id now resolves differently, triggers fresh.
    """
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

    # Re-attach decision. Only for an explicit run id: compare it to
    # the run id stashed when the edit cleared this operator's run.
    if run_id_template and run_id_arg:
        key = f"{node_id}::{task.task_id}"
        cleared = (cleared_run_ids or {}).get(key)
        if cleared is not None and str(cleared) == str(run_id_arg):
            # Same run id — re-attach to the cleared (re-running) run
            # rather than POSTing a duplicate, which Airflow rejects.
            return {
                "state": "success",
                "run_id": str(run_id_arg),
                "detail": f"re-attached to cleared run {run_id_arg}",
                "reattached": True,
            }

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


def _poll_task_state(
    task: CompiledExternalTask,
    hook: AirflowHook,
    resolve: Callable[[str], Any],
) -> dict[str, Any]:
    """Poll one Airflow task instance, succeeding when its raw state
    matches one of the configured target states.

    Matched against the *raw* Airflow state name (e.g. "deferred",
    "up_for_reschedule"), not the mapped state alphabet — so the
    sensor can advance the form on transitions other than success.
    Until the target is reached, surfaces `running` so the chain
    keeps polling.
    """
    cfg = task.config
    run_id = resolve(cfg["run_id_template"])
    ti = hook.get_task_instance(cfg["dag_id"], run_id, cfg["task_id"])
    raw_state = str(ti.get("state") or "").lower()
    targets = {s.lower() for s in cfg.get("target_states", [])}
    if raw_state in targets:
        return {
            "state": "success",
            "observed_state": raw_state,
            "detail": f"task reached state {raw_state!r}",
        }
    # Mapped failure modes (e.g. "failed", "upstream_failed") still
    # fail the sensor — an explicit `failed` target would already have
    # matched above; anything else terminal-bad fails loudly.
    mapped = _map_task_state(raw_state)
    if mapped == "failed":
        return {
            "state": "failed",
            "observed_state": raw_state,
            "detail": (
                f"task entered {raw_state!r} before reaching any of "
                f"{sorted(targets)!r}"
            ),
        }
    return {
        "state": "running",
        "observed_state": raw_state or "queued",
    }


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


def _send_hitl_response(
    task: CompiledExternalTask,
    hook: AirflowHook,
    resolve: Callable[[str], Any],
    form_values: dict[str, Any],
    steps_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST a response to a deferred Airflow HITL task. Called when a
    `HitlResponse` operator runs in the chain.

    Resolves `chosen_options` and `params_input` from their literal or
    `*_from` (StepRef descriptor) shapes in `cfg`. `params_input=None`
    means "send the submitting node's form_values as the payload."

    `steps_data` is the in-flight `{node_id: {<members>}}` namespace
    the chain processor builds — needed to resolve StepRef descriptors
    (which are `{node, name}` dicts, not template strings).
    """
    cfg = task.config
    run_id = resolve(cfg["run_id_template"])
    steps_data = steps_data or {}

    def _resolve_ref(ref: dict[str, Any]) -> Any:
        # A StepRef is a {node, name} descriptor. Look up the node;
        # if `name` is None it's a whole-node reference returning the
        # full node namespace, otherwise it's a member ref returning
        # that one value.
        node_data = steps_data.get(ref.get("node"))
        if not isinstance(node_data, dict):
            return None
        name = ref.get("name")
        if name is None:
            return dict(node_data)
        return node_data.get(name)

    # chosen_options: literal list, or a StepRef resolved at run.
    co_from = cfg.get("chosen_options_from")
    if co_from is not None:
        chosen = _resolve_ref(co_from)
        if not isinstance(chosen, list):
            chosen = [chosen] if chosen is not None else []
    else:
        chosen = list(cfg.get("chosen_options") or ["OK"])

    # params_input: literal dict, StepRef → resolved at run, or None
    # → fall back to the submitting node's form values.
    pi_from = cfg.get("params_input_from")
    if pi_from is not None:
        payload = _resolve_ref(pi_from)
        if not isinstance(payload, dict):
            payload = {}
    elif "params_input" in cfg and cfg["params_input"] is not None:
        payload = dict(cfg["params_input"])
    else:
        payload = dict(form_values)

    hook.respond_hitl(
        cfg["dag_id"], run_id, cfg["task_id"],
        chosen_options=chosen,
        params_input=payload,
    )
    return {
        "state": "success",
        "detail": f"posted HITL response to {cfg['task_id']!r}",
        "chosen_options": chosen,
        "params_input": payload,
    }


def respond_to_hitl(
    task: CompiledExternalTask,
    *,
    resolve: Callable[[str], Any],
    get_hook: Callable[[str], AirflowHook],
    chosen_options: list[str],
    params_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Submit a person's response to an Hitl task, resuming the
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
