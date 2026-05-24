"""
External-task operators. These represent work happening outside the
form's own runtime — an Airflow task, a webhook callback, a timer,
an arbitrary HTTP poll — that the form chain waits on before
advancing to the next step.

The base class declares the contract; subclasses bind it to specific
external systems. The runtime treats them uniformly: each has a
display id, each progresses through queued → running → success (or
failed) states, each blocks the chain until it reaches a terminal
state.

AirflowStatus is the original mock-only task sensor. The Airflow
operators below it — TriggerDag, TaskSensor, DagSensor,
XComPull — are the connection-backed family: each names a connection in
the connection store and is a graph-visible authored step. They run on
the mock progression until the runtime's real polling is wired; genuine
plumbing subclasses (Timer, WebhookWait) would stay graph-invisible.
"""

from __future__ import annotations

from .core import Operator


class ExternalTask(Operator):
    """Base for operators that represent external work the form is
    waiting on.

    Subclasses carry the configuration needed to identify what's being
    waited on (e.g., AirflowStatus carries `task_id`, `dag_id`, the
    templated `run_id`). The runtime knows how to map each subclass to
    its polling/observation strategy.

    Conceptually equivalent to Airflow's Sensor pattern, but the
    state alphabet is richer (queued / running / success / failed)
    so we can render progressive feedback in the chain UI rather
    than just a binary done/not-done.
    """

    kind = "external_task"

    # Whether this task is a meaningful, authored step that belongs in
    # the workflow graph. Plumbing tasks — timers, webhook waits — stay
    # False and behind the scenes; Airflow operators set it True.
    graph_visible = False

    # Whether a user may rerun *this operator on its own* from the UI.
    # When False, the operator's failed-state menu offers no per-step
    # rerun — but an upstream reset still cascades through it. Per-
    # instance overridable via the `retryable` constructor argument.
    retryable = True


class AirflowStatus(ExternalTask):
    """ExternalTask backed by an Airflow task instance.

    The runtime resolves `run_id` against the workflow's templating
    namespace at request time, then (in 9a, mocked; in the eventual
    Airflow-backed @backend pattern, real) queries the Airflow API
    for the task instance's current state.
    """

    # Distinct compile-time discriminator. The compiler / runtime use
    # `isinstance(op, ExternalTask)` for the broad routing and the
    # subclass identity (or this kind string) for specific polling
    # behavior.
    kind = "airflow_status"

    def __init__(
        self,
        *,
        task_id: str,
        dag_id: str,
        run_id: str,
    ) -> None:
        # Use the airflow task_id as the operator id, not the python
        # variable name. Two reasons:
        #   1. The task_id is what's authoritative — it's how Airflow
        #      identifies the task.
        #   2. Multiple nodes can reference the same airflow task
        #      (rare but possible); using task_id as the id makes that
        #      collision visible.
        super().__init__(id=task_id)
        self.task_id = task_id
        self.dag_id = dag_id
        self.run_id_template = run_id


class AirflowOperator(ExternalTask):
    """Base for operators that act on a real Airflow instance through a
    stored connection.

    Each is a node-internal step — authored downstream of a node's
    submit button, like an `@backend` call — and each is a first-class
    node in the workflow graph (`graph_visible`).

    `connection` names an entry in the connection store. When it's left
    out, the runtime falls back to mock progression, so a workflow can
    be built and demoed before any Airflow instance is wired up.

    Operators that produce a value downstream code references — the run
    id from TriggerDag, the pulled value from XComPull — expose it
    through the templating namespace as `steps.<operator id>.<field>`.
    """

    graph_visible = True


# Bounds for `poll_interval_ms` — between half a second (never DDoS
# the server) and one hour (anything slower is effectively "not
# polling"). The frontend takes the minimum across all in-flight
# operators as its refresh rate, so an outlier-fast operator drives
# the whole submission's traffic; the lower bound guards against
# that going to absurd values.
POLL_INTERVAL_MIN_MS = 500
POLL_INTERVAL_MAX_MS = 3_600_000


def _validate_poll_interval_ms(
    operator_id: str, value: "int | None",
) -> "int | None":
    """Type-and-range check for an operator's `poll_interval_ms`.
    None passes through (operator opts into the framework default,
    which lives in the frontend). A non-int or out-of-range value
    fails at compile time."""
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(
            f"operator {operator_id!r}: poll_interval_ms must be an "
            f"int (milliseconds); got {type(value).__name__}."
        )
    if value < POLL_INTERVAL_MIN_MS or value > POLL_INTERVAL_MAX_MS:
        raise ValueError(
            f"operator {operator_id!r}: poll_interval_ms must be "
            f"between {POLL_INTERVAL_MIN_MS} and {POLL_INTERVAL_MAX_MS} "
            f"ms; got {value}."
        )
    return value


class TriggerDag(AirflowOperator):
    """Trigger an Airflow DAG run, optionally passing a `conf` payload.

    `conf` values may be templated — `{"address": "{{ steps.intake.address }}"}`
    pulls a form value into the DAG run. The triggered run's id is
    exposed downstream as `steps.<id>.run_id`, which the sensors and
    XComPull reference to know which run to watch.

    `run_id` is optional and templated — pass `"{{ steps.intake.ref }}"`
    to set the DAG run's id from a form value. Airflow requires a run id
    to be unique within the DAG; a collision fails the trigger. When
    omitted, Airflow auto-generates one (`manual__<timestamp>`).
    """

    kind = "airflow_trigger_dag"

    def __init__(
        self,
        *,
        dag_id: str,
        conf: dict | None = None,
        run_id: str | None = None,
        connection: str | None = None,
        id: str | None = None,
        retryable: bool = True,
        waiting_message: str | None = None,
    ) -> None:
        super().__init__(id=id or f"trigger_{dag_id}")
        self.dag_id = dag_id
        self.conf = conf or {}
        self.run_id_template = run_id
        self.connection = connection
        self.retryable = retryable
        self.waiting_message = waiting_message


class TaskSensor(AirflowOperator):
    """Wait on a single Airflow task instance, surfacing its state in
    the form chain. The connection-backed successor to AirflowStatus.

    `run_id` is templated — typically `{{ steps.<trigger>.run_id }}` —
    so the sensor watches the run a TriggerDag step started.
    """

    kind = "airflow_task_sensor"

    def __init__(
        self,
        *,
        dag_id: str,
        task_id: str,
        run_id: str,
        connection: str | None = None,
        id: str | None = None,
        retryable: bool = True,
        waiting_message: str | None = None,
        poll_interval_ms: int | None = None,
    ) -> None:
        super().__init__(id=id or task_id)
        self.dag_id = dag_id
        self.task_id = task_id
        self.run_id_template = run_id
        self.connection = connection
        self.retryable = retryable
        self.waiting_message = waiting_message
        self.poll_interval_ms = _validate_poll_interval_ms(
            self.id or task_id, poll_interval_ms,
        )


class TaskStateSensor(AirflowOperator):
    """Wait until a specific Airflow task instance reaches one of a set
    of configured states. Useful for advancing the form on transitions
    other than `success` — e.g. when a HITL task reaches `deferred`,
    open the next form node so the user can supply the response data.

    `target_state` may be a single Airflow task state string (e.g.
    "deferred"), or a list of states — the sensor succeeds the first
    time the polled state matches any. Matched against the *raw*
    Airflow state name, not frontflow's mapped state alphabet, so
    "deferred" and "up_for_reschedule" are distinct.

    `run_id` is templated like the other sensors —
    `"{{ steps.<trigger>.run_id }}"` to follow a TriggerDag's run.

    `waiting_message` is rendered in the form's status panel while
    this operator polls. Templated against the form's `steps` namespace
    — `"Parsing {{ steps.upload.property_name }}…"` works.
    """

    kind = "airflow_task_state_sensor"

    def __init__(
        self,
        *,
        dag_id: str,
        task_id: str,
        run_id: str,
        target_state: "str | list[str]",
        connection: str | None = None,
        id: str | None = None,
        retryable: bool = True,
        waiting_message: str | None = None,
        poll_interval_ms: int | None = None,
    ) -> None:
        super().__init__(id=id or f"wait_{task_id}")
        self.dag_id = dag_id
        self.task_id = task_id
        self.run_id_template = run_id
        target_states = (
            [target_state] if isinstance(target_state, str)
            else list(target_state)
        )
        if not target_states:
            raise ValueError(
                f"TaskStateSensor {self.id!r} needs at least one "
                "target_state"
            )
        for s in target_states:
            if not isinstance(s, str) or not s:
                raise ValueError(
                    f"TaskStateSensor {self.id!r} target_state entries "
                    f"must be non-empty strings; got {s!r}"
                )
        self.target_states = target_states
        self.connection = connection
        self.retryable = retryable
        self.waiting_message = waiting_message
        self.poll_interval_ms = _validate_poll_interval_ms(
            self.id, poll_interval_ms,
        )


class DagSensor(AirflowOperator):
    """Wait on a whole Airflow DAG run, surfacing its overall state in
    the form chain — like TaskSensor, but for the run rather than
    one task.
    """

    kind = "airflow_dag_sensor"

    def __init__(
        self,
        *,
        dag_id: str,
        run_id: str,
        connection: str | None = None,
        id: str | None = None,
        retryable: bool = True,
        waiting_message: str | None = None,
        poll_interval_ms: int | None = None,
    ) -> None:
        super().__init__(id=id or f"dag_status_{dag_id}")
        self.dag_id = dag_id
        self.run_id_template = run_id
        self.connection = connection
        self.retryable = retryable
        self.waiting_message = waiting_message
        self.poll_interval_ms = _validate_poll_interval_ms(
            self.id, poll_interval_ms,
        )


class XComPull(AirflowOperator):
    """Pull an XCom value a DAG task pushed, into the form.

    The pulled value enters the templating namespace as
    `steps.<id>.value`, so later steps and displays can use it. `key`
    defaults to the task's return value.
    """

    kind = "airflow_xcom_pull"

    def __init__(
        self,
        *,
        dag_id: str,
        task_id: str,
        run_id: str,
        key: str = "return_value",
        connection: str | None = None,
        id: str | None = None,
        retryable: bool = True,
        waiting_message: str | None = None,
        poll_interval_ms: int | None = None,
    ) -> None:
        super().__init__(id=id or f"xcom_{task_id}")
        self.dag_id = dag_id
        self.task_id = task_id
        self.run_id_template = run_id
        self.key = key
        self.connection = connection
        self.retryable = retryable
        self.waiting_message = waiting_message
        self.poll_interval_ms = _validate_poll_interval_ms(
            self.id, poll_interval_ms,
        )


class Hitl(AirflowOperator):
    """A human-in-the-loop step backed by an Airflow HITL operator.

    An Airflow 3.1+ HITL task pauses its DAG and exposes a "Required
    Action" — a subject, a body, a set of options, and an optional
    parameter form. This operator turns the form itself into that
    action's response surface: while the DAG waits, the form renders the
    prompt; the user's answer is sent back and the DAG resumes.

    Unlike the sensors, this operator has a distinct waiting state —
    `awaiting_response` — during which the form shows the prompt. It
    leaves that state only once a response has been submitted.
    """

    kind = "airflow_hitl"

    def __init__(
        self,
        *,
        dag_id: str,
        task_id: str,
        run_id: str,
        connection: str | None = None,
        id: str | None = None,
        retryable: bool = True,
        waiting_message: str | None = None,
        poll_interval_ms: int | None = None,
    ) -> None:
        super().__init__(id=id or f"hitl_{task_id}")
        self.dag_id = dag_id
        self.task_id = task_id
        self.run_id_template = run_id
        self.connection = connection
        self.retryable = retryable
        self.waiting_message = waiting_message
        self.poll_interval_ms = _validate_poll_interval_ms(
            self.id, poll_interval_ms,
        )


class HitlBranch(Hitl):
    """A human-in-the-loop step that also routes the form's chain.

    Behaves exactly like Hitl — it polls an Airflow HITL task,
    renders the prompt, and submits the response — but once the human
    answers, it routes the *form* to a node based on what they chose.
    `routes` maps an option to a downstream node id:

        confirm = ...        # two nodes wired downstream
        revise  = ...
        review = HitlBranch(
            connection="prod_airflow", dag_id="publish_article",
            task_id="editor_review", run_id="{{ steps.trigger.run_id }}",
            routes={"Approve": "confirm", "Reject": "revise"},
        )
        submit >> ... >> review >> [confirm(), revise()]

    The branch fires on the first chosen option. An option with no
    `routes` entry falls through to the normal `>>` chain. Like
    @backend.branch, the target nodes must be wired downstream.
    """

    kind = "airflow_hitl_branch"

    def __init__(
        self,
        *,
        dag_id: str,
        task_id: str,
        run_id: str,
        routes: dict[str, str],
        connection: str | None = None,
        id: str | None = None,
        retryable: bool = True,
        waiting_message: str | None = None,
        poll_interval_ms: int | None = None,
    ) -> None:
        super().__init__(
            dag_id=dag_id, task_id=task_id, run_id=run_id,
            connection=connection, id=id, retryable=retryable,
            waiting_message=waiting_message,
            poll_interval_ms=poll_interval_ms,
        )
        self.routes = dict(routes)


class HitlResponse(AirflowOperator):
    """Submit a response to a deferred Airflow HITL task, resuming the
    DAG.

    Pair this with an upstream `TaskStateSensor` that watches the same
    task for `deferred`. The form opens, the user fills in the inputs,
    and submitting runs this operator — which POSTs the response to
    Airflow's HITL endpoint. The DAG's HITL task then completes and
    the run continues.

    `chosen_options` is the predefined option list Airflow's HITL
    operator expects (Airflow 3.1+). Defaults to `["OK"]` — most
    integrations have a single option and use `params_input` for the
    actual payload. Either a literal `list[str]` or a `StepRef`
    pointing at a node-internal backend or form field that returns
    a list.

    `params_input` is the keyed payload. Either a literal dict (sent
    as-is at runtime), or a `StepRef` pointing at a node-internal
    `@backend` whose return is the payload (`steps.<node>.<fn_name>`).
    When omitted, the form's submitted values for this node are sent
    directly.

    On edit, this operator clears the target HITL task instance so a
    re-submit posts a fresh response — same mechanism the other
    task-owning operators use.
    """

    kind = "airflow_hitl_response"

    def __init__(
        self,
        *,
        dag_id: str,
        task_id: str,
        run_id: str,
        chosen_options: "list[str] | object" = None,
        params_input: "dict | object" = None,
        connection: str | None = None,
        id: str | None = None,
        retryable: bool = True,
        waiting_message: str | None = None,
    ) -> None:
        # Deferred import — references.py imports core/external too.
        from .references import StepRef
        super().__init__(id=id or f"respond_{task_id}")
        self.dag_id = dag_id
        self.task_id = task_id
        self.run_id_template = run_id
        self.connection = connection
        self.retryable = retryable
        self.waiting_message = waiting_message
        # chosen_options: default ["OK"]; accept literal list or StepRef.
        if chosen_options is None:
            self.chosen_options = ["OK"]
        elif isinstance(chosen_options, (list, StepRef)):
            self.chosen_options = (
                chosen_options
                if isinstance(chosen_options, StepRef)
                else list(chosen_options)
            )
        else:
            raise TypeError(
                f"HitlResponse {self.id!r}: chosen_options must be a "
                f"list[str] or a StepRef; got "
                f"{type(chosen_options).__name__}"
            )
        # params_input: default None (means "send all form values");
        # accept literal dict or StepRef.
        if params_input is None:
            self.params_input = None
        elif isinstance(params_input, (dict, StepRef)):
            self.params_input = (
                params_input
                if isinstance(params_input, StepRef)
                else dict(params_input)
            )
        else:
            raise TypeError(
                f"HitlResponse {self.id!r}: params_input must be a "
                f"dict or a StepRef; got "
                f"{type(params_input).__name__}"
            )
