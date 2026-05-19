"""
Form submission runtime.

Holds submission state in memory and exposes operations the API layer
maps onto endpoints. A `Submission` represents one user's traversal of
a workflow's chain — analogous to an Airflow DagRun but in form-builder
terminology.

State machine per submission:
  - `steps` is a list of step-submissions in execution order. Each
    entry tracks whether its form has been submitted (and if so, what
    values + which button + what the @backend returned).
  - When the most recent step's form is submitted and enough time has
    elapsed for all its ExternalTask operators to finish, the runtime
    appends the next step to `steps` based on the branch decision (or
    workflow-definition call order).
  - When @backend.branch returns END, `terminated` is set; no further
    steps are appended; the submission's overall state becomes
    "success".

Submission-id generation:
  - If the workflow declares `submission_id="..."` in its @form
    decorator, the runtime renders that Jinja template against the
    initial form values from the trigger request.
  - Otherwise a 10-char nanoid (lowercase + digits) is generated.
  - Collisions raise a ValueError; the API layer turns that into a
    409 Conflict.

Timing for the mocked ExternalTask progression is fixed (see
QUEUED_INITIAL / RUN_DURATION below). It only applies in 9a; once
real polling is wired (per subclass — Airflow API, webhook callbacks,
etc.), this is replaced by per-kind dispatch.
"""

from __future__ import annotations

import random
import re
import string
import threading
from urllib.parse import quote
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .compile import (
    CompiledBackendStep,
    CompiledBlock,
    CompiledNode,
    CompiledPage,
    CompiledWorkflow,
)
from .airflow_dispatch import advance_airflow_task
from .airflow_hook import AirflowError, AirflowHook
from .core import END
from .status import Affected, NeedsInput, NeedsReview, StepStatus, Unaffected
from .templating import render
from . import uploads

# --- Tuning constants ------------------------------------------------------

QUEUED_INITIAL = 1.5
RUN_DURATION = 3.0

_NANOID_ALPHABET = string.ascii_lowercase + string.digits
_NANOID_LEN = 10

# Per the URL-safety policy: only [a-zA-Z0-9_-] are allowed in
# submission_ids. Anything else raises at start time so authors can't
# accidentally produce broken URLs from a misconfigured template.
_VALID_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


# --- State -----------------------------------------------------------------


@dataclass
class StepSubmission:
    """One step the user has reached during the submission. A step is
    either a HITL node (awaits a form submit) or a backend step (runs
    automatically when reached)."""

    node_id: str
    form_values: Optional[dict[str, Any]] = None
    button_clicked: Optional[str] = None
    started_at: Optional[datetime] = None
    submitted_at: Optional[datetime] = None
    backend_return: Any = None
    next_node_id: Optional[str] = None
    branch_taken_explicitly: bool = False
    # Set when a backend step raised — the submission then fails.
    error: Optional[str] = None
    # Edit-cascade status — how an upstream edit left this step. A
    # StepStatus class (Unaffected by default); see workflows.status.
    status: type = Unaffected
    # Per-external-task state for connected Airflow operators, keyed by
    # the external task's id. Each entry is the dispatch state dict —
    # `state` plus, where they apply, `run_id` / `value` / `detail`.
    # In-memory only for now; cross-restart persistence is a follow-up.
    external_state: dict = field(default_factory=dict)

    @property
    def is_submitted(self) -> bool:
        return self.submitted_at is not None


@dataclass
class EventRecord:
    """One entry in a submission's append-only lifecycle log. Buffered
    on the Submission as things happen; the persistence layer flushes
    new entries through to the `event` table."""

    type: str
    occurred_at: datetime
    node_id: Optional[str] = None
    page_id: Optional[str] = None
    payload: Optional[dict[str, Any]] = None


@dataclass
class Submission:
    """One user's in-flight or completed traversal of a workflow.

    `handle` is the stable internal key — assigned at creation, never
    changes, and how the submission is stored. `submission_id` is the
    workflow's *minted* identifier: null while the submission is still
    a session draft, set the moment the value it derives from becomes
    available (a uuid at the first submit when unconfigured). Only once
    `submission_id` exists is the submission persisted and resumable.

    `form_version_id` pins the submission to the form_version it ran
    against, so it always advances and renders on its own version.
    `events` is the append-only lifecycle log the persistence layer
    flushes to the database.
    """

    handle: str
    form_id: str
    started_at: datetime
    submission_id: Optional[str] = None
    form_version_id: int = 0
    steps: list[StepSubmission] = field(default_factory=list)
    terminated: bool = False
    # Set when a backend step raised — overall state becomes "failed".
    failed: bool = False
    # Set when the submission reaches a terminal/failed state.
    ended_at: Optional[datetime] = None
    events: list[EventRecord] = field(default_factory=list)
    # The node currently re-opened by an explicit Edit, if any — used
    # to offer a Cancel affordance and to apply the chosen edit scope.
    # Cleared when that node is re-submitted or the edit is cancelled.
    editing_node_id: Optional[str] = None
    # The scope chosen for the active edit: "cascade" runs the
    # dependency-aware cascade on re-submit; "node_only" re-submits the
    # edited node alone, leaving downstream steps as they are.
    edit_scope: str = "cascade"


# Module-level storage. One instance per backend process; replaced with
# Redis / Postgres in later steps. `_submissions` is keyed by `handle`;
# `_id_index` maps a minted `submission_id` back to its handle so the
# canonical id resolves once it exists.
_submissions: dict[str, Submission] = {}
_id_index: dict[str, str] = {}
_submissions_lock = threading.Lock()


# --- Id generation ---------------------------------------------------------


def _generate_nanoid() -> str:
    return "".join(random.choices(_NANOID_ALPHABET, k=_NANOID_LEN))


# Captures the leading `steps.<id>` reference in a template, so the
# minter can tell whether every step a submission_id depends on has run.
_STEPS_REF_RE = re.compile(r"steps\s*\.\s*([A-Za-z_]\w*)")


def _mint_submission_id(
    workflow: CompiledWorkflow,
    submission: Submission,
) -> Optional[str]:
    """Compute the submission's id *if its source data is now available*.

    Returns the id, or None when it can't be minted yet:

      - No `submission_id` template → the submission's handle (a uuid).
        Always available, so an unconfigured workflow mints on the
        first submit.
      - A template → the rendered value, but only once **every** step
        the template references has run. Until then, None — the
        submission stays a draft.

    Raises ValueError if, with all referenced steps present, the
    template still resolves to an empty or non-URL-safe value — a
    genuine misconfiguration that should fail loudly.
    """
    template = workflow.submission_id_template
    if template is None:
        return submission.handle

    refs = set(_STEPS_REF_RE.findall(template))
    steps_data = build_steps_with_workflow(workflow, submission)
    if not refs.issubset(steps_data.keys()):
        return None  # a step the id derives from hasn't run yet

    rendered = render(template, steps_data).strip()
    if not rendered:
        raise ValueError(
            f"submission_id template for workflow {workflow.id!r} "
            "resolved to empty even though every step it references "
            "has run — check the template points at fields that hold "
            "a value."
        )
    if not _VALID_ID_RE.match(rendered):
        raise ValueError(
            f"submission_id template for workflow {workflow.id!r} "
            f"resolved to {rendered!r}, which contains characters "
            "outside [A-Za-z0-9_-]. Add `| slugify` so it's URL-safe."
        )
    return rendered


def _try_register_id(
    workflow: CompiledWorkflow,
    submission: Submission,
) -> None:
    """Mint and register the submission id if it can be minted now.

    Idempotent — a no-op once the id exists. Called after every step
    submission and backend-step run, so the id appears the instant its
    source value does."""
    if submission.submission_id is not None:
        return
    minted = _mint_submission_id(workflow, submission)
    if minted is None:
        return
    with _submissions_lock:
        existing = _id_index.get(minted)
        if existing is not None and existing != submission.handle:
            raise ValueError(
                f"submission_id {minted!r} already exists — the "
                f"workflow {workflow.id!r} produced a colliding id. Use "
                "a more unique submission_id template."
            )
        submission.submission_id = minted
        if minted != submission.handle:
            _id_index[minted] = submission.handle
    _record_event(
        workflow, submission, "id_minted", payload={"submission_id": minted}
    )


# --- Event recording -------------------------------------------------------


def _record_event(
    workflow: CompiledWorkflow,
    submission: Submission,
    type_: str,
    *,
    node_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    occurred_at: Optional[datetime] = None,
) -> None:
    """Append a lifecycle event to the submission's log. `page_id` is
    derived from the node when it belongs to a page."""
    page = workflow.node_page.get(node_id) if node_id is not None else None
    submission.events.append(
        EventRecord(
            type=type_,
            occurred_at=occurred_at or datetime.now(timezone.utc),
            node_id=node_id,
            page_id=page.id if page is not None else None,
            payload=payload,
        )
    )


def _finalize_events(
    workflow: CompiledWorkflow, submission: Submission
) -> None:
    """Emit the terminal/failed event once the submission has reached
    that state. Idempotent — emits each at most once. Called at the end
    of every public runtime operation."""
    if submission.terminated and not any(
        e.type == "submission_terminated" for e in submission.events
    ):
        if submission.ended_at is None:
            submission.ended_at = datetime.now(timezone.utc)
        _record_event(
            workflow,
            submission,
            "submission_terminated",
            occurred_at=submission.ended_at,
        )
    if submission.failed and not any(
        e.type == "submission_failed" for e in submission.events
    ):
        if submission.ended_at is None:
            submission.ended_at = datetime.now(timezone.utc)
        err = next(
            (s.error for s in submission.steps if s.error), None
        )
        _record_event(
            workflow,
            submission,
            "submission_failed",
            payload={"error": err} if err else None,
            occurred_at=submission.ended_at,
        )


# --- Submission lifecycle --------------------------------------------------


def _step_def(workflow: CompiledWorkflow, node_id: str) -> Any:
    """Resolve a `StepSubmission.node_id` to its compiled definition — a
    CompiledNode (top-level or page section node) or a CompiledBackendStep.

    The runtime's step list is flat: section nodes of a page appear in it
    directly, so a node_id may name a section node that isn't a top-level
    workflow step."""
    n = workflow.all_nodes_by_id.get(node_id)
    if n is not None:
        return n
    s = workflow.by_id.get(node_id)
    if isinstance(s, CompiledBackendStep):
        return s
    raise KeyError(f"unknown step {node_id!r}")


def _descend(workflow: CompiledWorkflow, step_id: str) -> str:
    """Resolve a `>>` edge target to the id the runtime actually lands on.

    A workflow edge may point at a page; the user lands on that page's
    entry section node, never the page itself. Node and backend-step
    targets pass through unchanged."""
    target = workflow.by_id.get(step_id)
    if isinstance(target, CompiledPage):
        return target.entry_node_id
    return step_id


def start_submission(
    workflow: CompiledWorkflow,
    initial_form_values: dict[str, Any],
    form_version_id: int = 0,
) -> Submission:
    """Create a new submission for the workflow, immediately submitting
    the landing step's form with the values provided in the request.

    The submission is born as a session draft — keyed by a `handle`,
    `submission_id` still null. The id is minted here if it can be
    (unconfigured workflows, or a template that resolves from the
    landing step); otherwise it stays a draft until a later step
    supplies the value. `form_version_id` pins it to the form version
    it is running against."""
    handle = _generate_nanoid()
    now = datetime.now(timezone.utc)
    landing = workflow.landing_node()

    button_id = landing.buttons[0].id if landing.buttons else ""
    first_step = StepSubmission(
        node_id=landing.id,
        form_values=initial_form_values,
        button_clicked=button_id,
        started_at=now,
        submitted_at=now,
    )

    submission = Submission(
        handle=handle,
        form_id=workflow.id,
        started_at=now,
        form_version_id=form_version_id,
        steps=[first_step],
    )
    _record_event(workflow, submission, "submission_created", occurred_at=now)
    _record_event(
        workflow, submission, "step_started", node_id=landing.id,
        occurred_at=now,
    )
    _record_event(
        workflow, submission, "step_submitted", node_id=landing.id,
        occurred_at=now,
    )

    _execute_backend(workflow, submission, first_step)
    _route_next(workflow, submission, landing, first_step)
    _try_register_id(workflow, submission)
    _finalize_events(workflow, submission)

    with _submissions_lock:
        _submissions[handle] = submission
    return submission


def get_submission(key: str) -> Optional[Submission]:
    """Look up a submission by either its `handle` (used while it is a
    draft) or its minted `submission_id`."""
    with _submissions_lock:
        found = _submissions.get(key)
        if found is not None:
            return found
        handle = _id_index.get(key)
        return _submissions.get(handle) if handle is not None else None


def submit_step(
    workflow: CompiledWorkflow,
    submission: Submission,
    node_id: str,
    form_values: dict[str, Any],
    button_clicked: Optional[str],
) -> StepSubmission:
    """Submit the form for the currently-awaiting step.

    Stores form_values + button_clicked, runs the @backend function,
    computes the next step's id from the branch return value or
    definition order.
    """
    ng = workflow.all_nodes_by_id.get(node_id)
    if ng is None:
        if isinstance(workflow.by_id.get(node_id), CompiledBackendStep):
            raise ValueError(
                f"Step {node_id!r} is a backend step, not a form — it "
                "has nothing to submit."
            )
        raise KeyError(f"Unknown node {node_id!r}")

    # The awaiting step is normally the last, but after an edit it can
    # be an earlier draft (downstream steps still materialized). Locate
    # it by id among the unsubmitted steps.
    target_idx = next(
        (
            i
            for i, s in enumerate(submission.steps)
            if s.node_id == node_id and not s.is_submitted
        ),
        None,
    )
    if target_idx is None:
        if any(s.node_id == node_id for s in submission.steps):
            raise ValueError(f"Step {node_id!r} is already submitted")
        raise ValueError(
            f"Cannot submit {node_id!r}: it is not an awaiting step"
        )
    latest = submission.steps[target_idx]

    # A re-submit of a step that still has steps after it is an edit —
    # capture its prior state so the cascade can diff old vs new.
    is_edit = target_idx < len(submission.steps) - 1
    old_values = dict(latest.form_values or {}) if is_edit else {}
    old_backend_return = latest.backend_return if is_edit else None

    if len(ng.buttons) > 1:
        valid_ids = {b.id for b in ng.buttons}
        if button_clicked not in valid_ids:
            raise ValueError(
                f"Invalid button {button_clicked!r}; expected one of {valid_ids}"
            )
    elif len(ng.buttons) == 1:
        button_clicked = ng.buttons[0].id

    latest.form_values = form_values
    latest.button_clicked = button_clicked
    latest.submitted_at = datetime.now(timezone.utc)
    latest.status = Unaffected
    _record_event(
        workflow, submission, "step_submitted", node_id=node_id,
        occurred_at=latest.submitted_at,
    )

    _execute_backend(workflow, submission, latest)
    _route_next(workflow, submission, ng, latest)

    if is_edit:
        is_active_edit = node_id == submission.editing_node_id
        node_only = is_active_edit and submission.edit_scope == "node_only"
        if is_active_edit:
            # The explicitly-edited node is being committed — clear the
            # edit tracking regardless of scope.
            submission.editing_node_id = None
            submission.edit_scope = "cascade"

        tail_node = submission.steps[target_idx + 1].node_id
        if latest.next_node_id != tail_node:
            # The edit rerouted (a branch decided differently) — the
            # old downstream path is gone. Drop it; advance rebuilds.
            del submission.steps[target_idx + 1:]
            submission.terminated = False
            submission.failed = False
            submission.ended_at = None
        elif not node_only:
            # The edited step still routes into the existing tail —
            # decide each downstream step's fate from what changed.
            # ("node_only" scope skips this: downstream is left as-is.)
            apply_edit_cascade(
                workflow, submission, target_idx,
                old_values, old_backend_return,
            )
    _try_register_id(workflow, submission)
    _finalize_events(workflow, submission)
    return latest


def advance(workflow: CompiledWorkflow, submission: Submission) -> None:
    """Idempotent: progress the submission to reflect the wall clock.

    HITL nodes wait for a form submit (and for their external tasks to
    finish). Backend steps run automatically the moment they're reached
    — so a chain of them resolves within a single advance() call.
    Called before building submission responses.
    """
    while not submission.terminated and not submission.failed:
        # The frontier is the earliest unsubmitted step — normally the
        # last, but after an edit cascade an earlier step may be a
        # re-opened draft with submitted steps still after it.
        latest_idx = next(
            (
                i
                for i, s in enumerate(submission.steps)
                if not s.is_submitted
            ),
            len(submission.steps) - 1,
        )
        latest = submission.steps[latest_idx]
        step_def = _step_def(workflow, latest.node_id)

        if isinstance(step_def, CompiledBackendStep):
            # Backend steps run the instant they're reached.
            if not latest.is_submitted:
                _run_backend_step(workflow, submission, latest, step_def)
                if submission.failed:
                    break
                _record_event(
                    workflow, submission, "step_submitted",
                    node_id=latest.node_id, occurred_at=latest.submitted_at,
                )
                # A backend step's return value may itself be the
                # submission_id's source — mint now if it just became
                # available.
                _try_register_id(workflow, submission)
                if not _route_next(workflow, submission, step_def, latest):
                    break  # routing failed → submission failed
            # Backend steps have no external tasks — fall through.
        else:
            # CompiledNode — a HITL screen.
            if not latest.is_submitted:
                if not step_def.buttons:
                    # A buttonless node is the workflow's final screen
                    # (the compiler guarantees it). There's nothing to
                    # submit — reaching it completes the submission.
                    latest.form_values = {}
                    latest.submitted_at = datetime.now(timezone.utc)
                    _record_event(
                        workflow, submission, "step_submitted",
                        node_id=latest.node_id,
                        occurred_at=latest.submitted_at,
                    )
                    submission.terminated = True
                    break
                break  # Awaiting input

            # Trailing ExternalTask operators run only when the branch
            # decision was natural (fall through). Explicit branches —
            # @backend.branch returning a specific id or END — skip
            # the trailing tasks, mirroring Airflow's BranchPythonOperator.
            if not latest.branch_taken_explicitly:
                ext = step_def.external_tasks
                if chain_is_real(ext):
                    # Connected Airflow operators — real polling.
                    if not _process_real_chain(
                        workflow, submission, latest, ext
                    ):
                        break  # chain still running, or it failed
                else:
                    # Legacy / connectionless tasks — mock progression.
                    elapsed = (
                        datetime.now(timezone.utc) - latest.submitted_at
                    ).total_seconds()
                    if elapsed < _total_external_task_time(len(ext)):
                        break

        if latest.next_node_id is None:
            # No next step. If this is genuinely the last materialized
            # step, the submission ends; otherwise (a re-opened step
            # mid-list during a cascade) just move on.
            if latest_idx == len(submission.steps) - 1:
                submission.terminated = True
                break
            continue

        try:
            _step_def(workflow, latest.next_node_id)
        except KeyError as e:
            raise RuntimeError(
                f"Step target {latest.next_node_id!r} not in workflow"
            ) from e

        nxt_idx = latest_idx + 1
        if nxt_idx < len(submission.steps):
            # A step is already materialized after this one. If it's
            # the step routing leads to, continue into it; if routing
            # diverged (a branch rerouted), the old tail is stale —
            # drop it and materialize the new target.
            if submission.steps[nxt_idx].node_id == latest.next_node_id:
                continue
            del submission.steps[nxt_idx:]

        now = datetime.now(timezone.utc)
        submission.steps.append(
            StepSubmission(node_id=latest.next_node_id, started_at=now)
        )
        _record_event(
            workflow, submission, "step_started",
            node_id=latest.next_node_id, occurred_at=now,
        )

    _finalize_events(workflow, submission)


# --- Edit cascade ----------------------------------------------------------
#
# When an already-submitted step is re-submitted (an edit), the cascade
# decides what that change does to every downstream step. Each step is
# assigned a StepStatus: Unaffected (keep as-is), NeedsReview (still
# valid, flag it), or NeedsInput (must be re-confirmed). See
# workflows.status and the dependency graph in compile.StepDep.


def _ref_hit(node: str, field: Optional[str],
             changed: set[tuple[str, Optional[str]]]) -> bool:
    """Whether a `(node, field)` reference is touched by the `changed`
    set. A field reference matches its exact pair; a whole-node
    reference (`field is None`) matches any change in that node."""
    if field is None:
        return any(n == node for n, _ in changed)
    return (node, field) in changed


def _diff_values(
    old: dict[str, Any], new: dict[str, Any]
) -> set[str]:
    """Field ids whose value differs between two form-value dicts —
    added or removed keys included."""
    return {
        k for k in set(old) | set(new) if old.get(k) != new.get(k)
    }


def _options_bites(
    dep: "StepDep",
    step: StepSubmission,
    steps_data: dict[str, Any],
) -> bool:
    """For an `options` dependency whose source changed: True if the
    step's submitted choice is no longer valid (must re-pick →
    NeedsInput), False if it still sits in the new option set (the set
    shifted but the answer holds → NeedsReview)."""
    new_options = _resolve_step_ref(
        {"node": dep.node, "name": dep.field}, steps_data
    )
    if not isinstance(new_options, list):
        return True  # can't resolve a set — be safe, treat as breaking
    value = (step.form_values or {}).get(dep.local)
    if value is None or value == "" or value == []:
        return False  # nothing chosen — nothing to invalidate
    chosen = value if isinstance(value, list) else [value]
    return any(c not in new_options for c in chosen)


def _when_outcomes(
    block: CompiledBlock, steps_data: dict[str, Any]
) -> dict[int, bool]:
    """Evaluate every `When` block in a layout against `steps_data`,
    keyed by the block's identity. Only the cross-node conditions are
    evaluated here — same-node conditions resolve in the browser and
    can't move on an upstream edit."""
    out: dict[int, bool] = {}

    def walk(b: CompiledBlock) -> None:
        if b.type == "when":
            cross = [
                c for c in b.props.get("conditions", [])
                if isinstance(c, dict) and "node" in c
            ]
            if cross:
                out[id(b)] = all(
                    _eval_cross_condition(c, steps_data) for c in cross
                )
        for child in b.children:
            walk(child)

    walk(block)
    return out


def _condition_bites(
    node: CompiledNode,
    old_steps_data: dict[str, Any],
    new_steps_data: dict[str, Any],
) -> bool:
    """True if any cross-node `When` block in the node flips its
    outcome between the old and new step data — a flip changes which
    fields are shown, so the node's submitted data may no longer fit."""
    before = _when_outcomes(node.layout, old_steps_data)
    after = _when_outcomes(node.layout, new_steps_data)
    return any(before.get(k) != after.get(k) for k in before)


def _status_for_step(
    workflow: CompiledWorkflow,
    step: StepSubmission,
    changed: set[tuple[str, Optional[str]]],
    steps_data: dict[str, Any],
    old_steps_data: dict[str, Any],
) -> type:
    """The StepStatus a single downstream step earns against the
    `changed` set — the strictest of its per-dependency verdicts."""
    step_def = _step_def(workflow, step.node_id)

    # Gather (kind-bearing) dependencies for this step.
    verdicts: list[type] = []

    if isinstance(step_def, CompiledBackendStep):
        # A backend step's dependencies are its explicit `steps`
        # arguments. Any hit means it must re-run.
        refs = list(step_def.arg_refs) + list(step_def.kwarg_refs.values())
        for ref in refs:
            if _ref_hit(ref.get("node"), ref.get("name"), changed):
                verdicts.append(NeedsInput)
        return StepStatus.strictest(verdicts) if verdicts else Unaffected

    # A HITL node — walk its declared StepDeps.
    condition_checked = False
    for dep in step_def.deps:
        if not _ref_hit(dep.node, dep.field, changed):
            continue
        if dep.source in ("template", "default"):
            verdicts.append(NeedsReview)
        elif dep.source == "options":
            verdicts.append(
                NeedsInput if _options_bites(dep, step, steps_data)
                else NeedsReview
            )
        elif dep.source == "condition" and not condition_checked:
            # Re-evaluate the node's cross-node `When` blocks old vs
            # new; a flipped outcome means the visible field set
            # changed. Done once — it inspects the whole node.
            condition_checked = True
            if _condition_bites(step_def, old_steps_data, steps_data):
                verdicts.append(NeedsInput)
    return StepStatus.strictest(verdicts) if verdicts else Unaffected


def compute_step_statuses(
    workflow: CompiledWorkflow,
    submission: Submission,
    edited_index: int,
    old_values: dict[str, Any],
    old_backend_return: Any,
) -> dict[int, type]:
    """Decide, for every step after `edited_index`, the StepStatus that
    the edit at `edited_index` leaves it in.

    `old_values` / `old_backend_return` are the edited step's state
    *before* this re-submit (its new state is already on the step).
    Returns a map of step-index → StepStatus for the downstream steps;
    the caller applies it. A step that earns NeedsInput has its own
    outputs become uncertain, so its fields join the change set and
    later dependants are caught transitively."""
    edited = submission.steps[edited_index]
    new_values = edited.form_values or {}

    # The initial change set: the edited step's differing fields, plus
    # its node-internal @backend return if that moved.
    changed: set[tuple[str, Optional[str]]] = {
        (edited.node_id, f) for f in _diff_values(old_values, new_values)
    }
    edited_def = _step_def(workflow, edited.node_id)
    if (
        isinstance(edited_def, CompiledNode)
        and edited_def.backend_call is not None
        and old_backend_return != edited.backend_return
    ):
        changed.add((edited.node_id, edited_def.backend_call.fn.name))

    # Step data as it was before the edit (for re-evaluating conditions)
    # and as it is now.
    old_steps_data = build_steps_with_workflow(workflow, submission)
    # old_steps_data reflects post-edit values for the edited node; swap
    # the edited node's slice back to its pre-edit values.
    if isinstance(old_steps_data.get(edited.node_id), dict):
        old_steps_data = dict(old_steps_data)
        old_steps_data[edited.node_id] = dict(old_values)
    new_steps_data = build_steps_with_workflow(workflow, submission)

    result: dict[int, type] = {}
    for idx in range(edited_index + 1, len(submission.steps)):
        step = submission.steps[idx]
        if not step.is_submitted:
            # Already a draft from an earlier wave — a draft *is*
            # needs_input until the user re-submits it. Don't re-judge
            # it, but its outputs are still uncertain, so propagate.
            status: type = NeedsInput
        else:
            status = _status_for_step(
                workflow, step, changed, new_steps_data, old_steps_data
            )
        result[idx] = status
        if issubclass(status, NeedsInput):
            # This step's outputs are now uncertain — propagate so
            # anything reading them downstream is caught too.
            for f in step.form_values or {}:
                changed.add((step.node_id, f))
            sd = _step_def(workflow, step.node_id)
            if isinstance(sd, CompiledNode) and sd.backend_call is not None:
                changed.add((step.node_id, sd.backend_call.fn.name))
            elif isinstance(sd, CompiledBackendStep):
                changed.add((step.node_id, sd.fn.name))
    return result


def apply_edit_cascade(
    workflow: CompiledWorkflow,
    submission: Submission,
    edited_index: int,
    old_values: dict[str, Any],
    old_backend_return: Any,
) -> None:
    """Apply the edit cascade to the steps after `edited_index`.

    Runs `compute_step_statuses`, then rewrites each downstream step in
    place: an `Unaffected` or `NeedsReview` step keeps its submitted
    values (its `status` records which); a `NeedsInput` step is
    re-opened as a draft — `submitted_at` cleared, its prior values
    kept for pre-fill — so the user re-confirms it. `advance` then
    drains forward, stopping at the first re-opened step.

    The edited step itself is left as freshly submitted (`Unaffected`).
    """
    statuses = compute_step_statuses(
        workflow, submission, edited_index, old_values, old_backend_return
    )
    submission.steps[edited_index].status = Unaffected
    submission.terminated = False
    submission.failed = False
    submission.ended_at = None
    for idx, status in statuses.items():
        step = submission.steps[idx]
        step.status = status
        if issubclass(status, NeedsInput):
            # Re-open: drop the submission mark, keep values as a draft.
            step.submitted_at = None
            step.button_clicked = step.button_clicked
            step.backend_return = None
            step.next_node_id = None
            step.branch_taken_explicitly = False
            step.error = None
    _record_event(
        workflow, submission, "step_reset",
        node_id=submission.steps[edited_index].node_id,
        payload={
            "cascade": {
                submission.steps[i].node_id: s.key
                for i, s in statuses.items()
            }
        },
    )


def clear_submission_from(
    workflow: CompiledWorkflow,
    submission: Submission,
    from_node_id: Optional[str],
    mode: str = "reset",
    scope: str = "cascade",
) -> list[str]:
    """Rewind the submission from the given step forward (or restart it
    entirely if `from_node_id` is None).

    `mode` controls how the target step is re-opened:
      - "reset" — empty, as if never filled;
      - "edit"  — pre-filled with its previously-submitted answers, as
        an editable draft. `mode` is ignored for a full restart
        (`from_node_id` is None), which always replays the landing.

    Returns the list of node ids that were affected. Cascades:
    everything downstream of the target is dropped, the target becomes
    the new latest (unsubmitted), and `terminated` is reset.
    """
    if from_node_id is None:
        first = submission.steps[0] if submission.steps else None
        affected = [s.node_id for s in submission.steps]
        submission.steps = []
        submission.terminated = False
        submission.failed = False
        submission.ended_at = None
        submission.editing_node_id = None
        submission.edit_scope = "cascade"
        _record_event(
            workflow, submission, "step_reset",
            payload={"affected": affected, "from": None},
        )
        if first is not None and first.form_values is not None:
            now = datetime.now(timezone.utc)
            submission.started_at = now
            landing_id = workflow.landing_node().id
            restarted = StepSubmission(
                node_id=landing_id,
                form_values=first.form_values,
                button_clicked=first.button_clicked,
                started_at=now,
                submitted_at=now,
            )
            _execute_backend(workflow, submission, restarted)
            _route_next(
                workflow, submission, workflow.landing_node(), restarted
            )
            submission.steps = [restarted]
            _record_event(
                workflow, submission, "step_started", node_id=landing_id,
                occurred_at=now,
            )
            _record_event(
                workflow, submission, "step_submitted", node_id=landing_id,
                occurred_at=now,
            )
        return affected

    target_idx = next(
        (
            i
            for i, s in enumerate(submission.steps)
            if s.node_id == from_node_id
        ),
        None,
    )
    if target_idx is None:
        return []

    affected = [s.node_id for s in submission.steps[target_idx:]]
    target = submission.steps[target_idx]
    now = datetime.now(timezone.utc)

    if mode == "edit":
        # Re-open the target in place — keep its answers as a draft and
        # leave every downstream step materialized. Re-submitting the
        # target then runs the edit cascade, which decides each
        # downstream step's fate from what actually changed.
        target.submitted_at = None
        target.backend_return = None
        target.next_node_id = None
        target.branch_taken_explicitly = False
        target.error = None
        target.status = NeedsInput
        submission.terminated = False
        submission.failed = False
        submission.ended_at = None
        submission.editing_node_id = from_node_id
        submission.edit_scope = (
            "node_only" if scope == "node_only" else "cascade"
        )
        _record_event(
            workflow, submission, "step_reset",
            node_id=from_node_id,
            payload={"from": from_node_id, "mode": "edit", "scope": scope},
        )
        return [from_node_id]

    # reset — drop the target and everything after it, re-open empty.
    submission.steps = submission.steps[:target_idx]
    submission.terminated = False
    submission.failed = False
    submission.ended_at = None
    submission.editing_node_id = None
    submission.edit_scope = "cascade"
    submission.steps.append(
        StepSubmission(node_id=from_node_id, started_at=now)
    )
    _record_event(
        workflow, submission, "step_reset",
        node_id=from_node_id,
        payload={"affected": affected, "from": from_node_id, "mode": mode},
    )
    _record_event(
        workflow, submission, "step_started", node_id=from_node_id,
        occurred_at=now,
    )
    return affected


# --- Persistence bridge ----------------------------------------------------
#
# The runtime owns the in-memory types; these translate to/from the plain
# dict the store layer reads and writes. The runtime never imports the
# store — the API layer ferries snapshots between them.

_JSONABLE = (str, int, float, bool, type(None), dict, list)


def _jsonable(value: Any) -> Any:
    """Coerce a value to something the JSON store can hold. Plain data
    passes through; sentinels and objects (e.g. a branch's END return)
    become None — only ever true of terminal steps, whose return is
    not read downstream."""
    return value if isinstance(value, _JSONABLE) else None


def _step_kind(workflow: CompiledWorkflow, node_id: str) -> str:
    return (
        "backend"
        if isinstance(workflow.by_id.get(node_id), CompiledBackendStep)
        else "node"
    )


def submission_snapshot(
    workflow: CompiledWorkflow, submission: Submission
) -> dict[str, Any]:
    """Build the plain-dict snapshot the store layer persists. Derives
    each step's page membership and kind from the workflow."""
    state = (
        "failed"
        if submission.failed
        else "success"
        if submission.terminated
        else "running"
    )
    error = next((s.error for s in submission.steps if s.error), None)

    steps: list[dict[str, Any]] = []
    for i, s in enumerate(submission.steps):
        page = workflow.node_page.get(s.node_id)
        steps.append(
            {
                "seq": i,
                "node_id": s.node_id,
                "page_id": page.id if page is not None else None,
                "kind": _step_kind(workflow, s.node_id),
                "state": (
                    "failed"
                    if s.error
                    else "submitted"
                    if s.is_submitted
                    else "awaiting"
                ),
                "started_at": s.started_at,
                "submitted_at": s.submitted_at,
                "form_values": s.form_values,
                "backend_return": _jsonable(s.backend_return),
                "button_clicked": s.button_clicked,
                "next_node_id": s.next_node_id,
                "branch_explicit": s.branch_taken_explicitly,
                "status": s.status.key,
                # Per-operator Airflow state — must persist so a
                # triggered run id survives a reload and a trigger
                # never re-fires.
                "external_state": _jsonable(s.external_state),
            }
        )

    return {
        "handle": submission.handle,
        "submission_id": submission.submission_id,
        "form_version_id": submission.form_version_id,
        "state": state,
        "created_at": submission.started_at,
        "terminated_at": submission.ended_at,
        "error": error,
        "editing_node_id": submission.editing_node_id,
        "edit_scope": submission.edit_scope,
        "steps": steps,
        "events": [
            {
                "type": e.type,
                "node_id": e.node_id,
                "page_id": e.page_id,
                "occurred_at": e.occurred_at,
                "payload": e.payload,
            }
            for e in submission.events
        ],
    }


def hydrate_submission(snapshot: dict[str, Any], form_id: str) -> Submission:
    """Reconstruct a Submission from a stored snapshot and register it
    in the in-memory working set. Used at boot to rehydrate state."""
    steps = [
        StepSubmission(
            node_id=s["node_id"],
            form_values=s["form_values"],
            button_clicked=s["button_clicked"],
            started_at=s["started_at"],
            submitted_at=s["submitted_at"],
            backend_return=s["backend_return"],
            next_node_id=s["next_node_id"],
            branch_taken_explicitly=s["branch_explicit"],
            error=s["state"] == "failed" and snapshot["error"] or None,
            status=StepStatus.parse(s.get("status") or "unaffected"),
            external_state=s.get("external_state") or {},
        )
        for s in snapshot["steps"]
    ]
    submission = Submission(
        handle=snapshot["handle"],
        form_id=form_id,
        started_at=snapshot["created_at"],
        submission_id=snapshot["submission_id"],
        form_version_id=snapshot["form_version_id"],
        steps=steps,
        terminated=snapshot["state"] == "success",
        failed=snapshot["state"] == "failed",
        ended_at=snapshot["terminated_at"],
        editing_node_id=snapshot.get("editing_node_id"),
        edit_scope=snapshot.get("edit_scope") or "cascade",
        events=[
            EventRecord(
                type=e["type"],
                occurred_at=e["occurred_at"],
                node_id=e["node_id"],
                page_id=e["page_id"],
                payload=e["payload"],
            )
            for e in snapshot["events"]
        ],
    )
    with _submissions_lock:
        _submissions[submission.handle] = submission
        sid = submission.submission_id
        if sid is not None and sid != submission.handle:
            _id_index[sid] = submission.handle
    return submission


# --- Internals -------------------------------------------------------------


def _execute_backend(
    workflow: CompiledWorkflow,
    submission: Submission,
    step: StepSubmission,
) -> None:
    """Invoke a node's @backend function, if any. Operator args are
    bound positionally; a `steps` parameter, if declared, receives the
    cross-step accessor."""
    ng = workflow.all_nodes_by_id.get(step.node_id)
    if ng is None or ng.backend_call is None:
        return
    bc = ng.backend_call

    # Build the function arguments. For each arg op id:
    #   - Button ids → boolean (True if this button was clicked)
    #   - Input / widget field names → submitted form value
    #   - File / S3File field names → an upload handle (.read(), etc.)
    button_ids = {b.id for b in ng.buttons}
    file_field_types = {
        f.name: f.type
        for f in ng.fields
        if f.type in ("file", "s3file")
    }
    args: list[Any] = []
    for arg_id in bc.arg_op_ids:
        if arg_id in button_ids:
            args.append(arg_id == step.button_clicked)
        elif arg_id in file_field_types:
            raw = (step.form_values or {}).get(arg_id)
            args.append(
                uploads.handle_for_value(
                    file_field_types[arg_id], raw
                )
            )
        else:
            args.append((step.form_values or {}).get(arg_id))

    kwargs: dict[str, Any] = {}
    if "steps" in bc.fn.param_names:
        kwargs["steps"] = _steps_accessor(workflow, submission)

    try:
        result = bc.fn.func(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        # A node-attached backend that raises marks the step (and the
        # submission) failed — the same outcome as a standalone backend
        # step or an Airflow task failing. The submission still exists
        # in `failed` state, so it can be inspected and retried with a
        # full reset; it never crashes the request with a 500.
        step.error = (
            f"@backend {bc.fn.name!r} in node "
            f"{step.node_id!r} raised: {type(e).__name__}: {e}"
        )
        submission.failed = True
        submission.ended_at = datetime.now(timezone.utc)
        return
    step.backend_return = result


def _run_backend_step(
    workflow: CompiledWorkflow,
    submission: Submission,
    step: StepSubmission,
    step_def: CompiledBackendStep,
) -> None:
    """Execute a workflow-level backend step. Its arguments are `steps`
    references — resolved against the submission's accumulated data and
    bound positionally / by keyword to the function's parameters. On
    failure, the error is recorded and the submission fails."""
    fn = step_def.fn
    steps_data = build_steps_with_workflow(workflow, submission)
    args = [_resolve_step_ref(r, steps_data) for r in step_def.arg_refs]
    kwargs = {
        k: _resolve_step_ref(r, steps_data)
        for k, r in step_def.kwarg_refs.items()
    }

    now = datetime.now(timezone.utc)
    try:
        result = fn.func(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        step.error = f"{type(e).__name__}: {e}"
        step.submitted_at = now
        submission.failed = True
        return
    step.backend_return = result
    step.submitted_at = now


def _determine_next(
    workflow: CompiledWorkflow,
    step_def: Any,
    step: StepSubmission,
) -> tuple[Optional[str], bool]:
    """Resolve the next step id from the `>>` graph and the branch
    decision.

    Returns (next_id, branched_explicitly):
      - next_id: a node/backend-step id to go to, or None to terminate
      - branched_explicitly: True when an @backend.branch chose a target
        explicitly (an id or END) — trailing ExternalTasks then skip.

    For a page section node, routing follows the node's *page-internal*
    edges; a terminal section node ends the page and routing follows the
    *page's* workflow edges. Any target that resolves to a page is
    descended to that page's entry section node.

    Raises ValueError when a branch returns an id that is not wired
    downstream, or when a step fans out without a branch choosing one.
    """
    is_branch = False
    fn_name = ""
    # A node whose branch authority is an AirflowHitlBranch operator
    # routes *later* — when the operator resolves, not at submit time.
    # _determine_next must defer: the route is set by the chain handler
    # (_apply_hitl_branch_route) once the human has answered.
    hitl_branch_node = False
    if isinstance(step_def, CompiledBackendStep):
        downstream: list[str] = list(step_def.downstream)
        is_branch = step_def.is_branch
        fn_name = step_def.fn.name
    else:
        # CompiledNode — top-level or a page section node.
        hitl_branch_node = any(
            t.kind == "airflow_hitl_branch"
            for t in step_def.external_tasks
        )
        page = workflow.node_page.get(step_def.id)
        if page is not None and not step_def.downstream:
            # Terminal section node — the page completes here; route via
            # the page's own workflow-level edges.
            downstream = list(page.downstream)
        else:
            # Top-level node, or a non-terminal section node (its edges
            # are page-internal, to sibling section nodes).
            downstream = list(step_def.downstream)
            if step_def.backend_call is not None:
                is_branch = step_def.backend_call.fn.is_branch
                fn_name = step_def.backend_call.fn.name

    # The HITL-branch route isn't known yet — leave next_node_id unset;
    # _apply_hitl_branch_route fills it once the operator resolves.
    if hitl_branch_node:
        return None, False

    if is_branch:
        rv = step.backend_return
        if rv is END:
            return None, True
        if isinstance(rv, str):
            if rv not in downstream:
                raise ValueError(
                    f"@backend.branch {fn_name!r} returned {rv!r}, but "
                    f"{rv!r} is not wired downstream of it (downstream: "
                    f"{downstream or '[]'}). Workflow edges must be "
                    f"explicit — wire it, e.g. "
                    f"`{step_def.id}() >> [{rv}(), ...]`."
                )
            return _descend(workflow, rv), True
        # rv is None → fall through to the (single) downstream step.

    # Fall-through: a plain step, or a branch that returned None.
    if not downstream:
        return None, False
    if len(downstream) == 1:
        return _descend(workflow, downstream[0]), False
    raise ValueError(
        f"step {step_def.id!r} has multiple downstream steps "
        f"({downstream}) but did not choose one — a @backend.branch must "
        "return one of them (or END)."
    )


def _route_next(
    workflow: CompiledWorkflow,
    submission: Submission,
    step_def: Any,
    step: StepSubmission,
) -> bool:
    """Resolve and record the next step. On a routing error, record it
    on the step and fail the submission. Returns False when it failed."""
    try:
        next_id, explicit = _determine_next(workflow, step_def, step)
    except ValueError as e:
        step.error = f"routing failed: {e}"
        submission.failed = True
        return False
    step.next_node_id = next_id
    step.branch_taken_explicitly = explicit
    return True


def chain_is_real(external_tasks: list) -> bool:
    """A node's external-task chain runs against a real Airflow instance
    only when every task is a connected Airflow operator. Any legacy or
    connectionless task drops the whole chain to mock progression — so a
    workflow still runs before any connection is wired up."""
    return bool(external_tasks) and all(
        (t.config or {}).get("connection") for t in external_tasks
    )


def _airflow_hook_for(name: str) -> AirflowHook:
    """Build an AirflowHook from a stored connection. Imported lazily so
    the runtime carries no module-load dependency on the store."""
    from . import store

    rec = store.get_connection(name)
    if rec is None:
        raise AirflowError(f"connection {name!r} is not configured")
    return AirflowHook(rec)


def _process_real_chain(
    workflow: CompiledWorkflow,
    submission: Submission,
    step: StepSubmission,
    external_tasks: list,
) -> bool:
    """Advance a node's chain of connected Airflow operators by one tick.

    Tasks are processed in chain order; each is dispatched once per call
    and processing stops at the first task not yet successful — so a
    sensor is polled once, a trigger fires once. Returns True when the
    whole chain has succeeded. A failed task fails the submission.
    """
    steps_data = build_steps_with_workflow(workflow, submission)

    def resolve(template_str: str) -> Any:
        return render(template_str, steps_data)

    for task in external_tasks:
        prior = step.external_state.get(task.task_id)
        if prior and prior.get("state") == "success":
            # Already done — but re-apply a HITL branch's routing every
            # pass, so a refresh doesn't lose the chosen target.
            _apply_hitl_branch_route(workflow, step, task, prior)
            continue
        new_state = advance_airflow_task(
            task, prior, resolve=resolve, get_hook=_airflow_hook_for,
        )
        step.external_state[task.task_id] = new_state
        # A finished task's outputs (a run id, a pulled value) feed the
        # templates of the tasks after it in the same pass.
        steps_data[task.task_id] = new_state

        state = new_state.get("state")
        if state == "failed":
            submission.failed = True
            submission.ended_at = datetime.now(timezone.utc)
            # Surface the failure on the step itself — otherwise the
            # submission fails silently and the form shows a dead end
            # with no explanation.
            detail = new_state.get("detail") or "no detail"
            step.error = (
                f"Airflow task {task.task_id!r} failed: {detail}"
            )
            _record_event(
                workflow, submission, "submission_failed",
                node_id=step.node_id,
                occurred_at=submission.ended_at,
                payload={
                    "external_task": task.task_id,
                    "detail": new_state.get("detail"),
                },
            )
            return False
        if state != "success":
            return False  # still running — leave the rest for later

        # An AirflowHitlBranch task that just succeeded routes the form.
        _apply_hitl_branch_route(workflow, step, task, new_state)
    return True


def _apply_hitl_branch_route(
    workflow: CompiledWorkflow,
    step: StepSubmission,
    task: Any,
    task_state: dict[str, Any],
) -> None:
    """Route the form's chain on an AirflowHitlBranch task's outcome.

    The first chosen option is looked up in the operator's `routes` map;
    a hit sets next_node_id (an explicit branch, like @backend.branch).
    An unmapped option falls through to the normal `>>` chain. A no-op
    for any task that isn't an AirflowHitlBranch.
    """
    if task.kind != "airflow_hitl_branch":
        return
    routes = (task.config or {}).get("routes") or {}
    chosen = (task_state.get("chosen_options") or [None])[0]
    target = routes.get(chosen)
    if target is not None:
        step.next_node_id = _descend(workflow, target)
        step.branch_taken_explicitly = True


def _total_external_task_time(num_tasks: int) -> float:
    if num_tasks == 0:
        return 0.0
    return QUEUED_INITIAL + RUN_DURATION * num_tasks


def external_task_states(num_tasks: int, elapsed: float) -> list[tuple[int, str]]:
    """For an ordered list of `num_tasks` mock external tasks, return
    the (index, state) of every task that should be visible at
    `elapsed` seconds since form submission. Tasks not yet started
    are omitted entirely (matches Airflow behavior of not materializing
    downstream task instances until their upstream succeeds).

    In 9a, this models any ExternalTask uniformly: the queued/running/
    success transition is purely a function of elapsed time. Real
    polling (Airflow, Webhook, etc.) replaces this with subclass-
    specific state queries in later steps.
    """
    if num_tasks <= 0 or elapsed < 0:
        return []

    states: list[tuple[int, str]] = []
    if elapsed < QUEUED_INITIAL:
        return [(0, "queued")]
    if elapsed < QUEUED_INITIAL + RUN_DURATION:
        return [(0, "running")]
    states.append((0, "success"))

    for n in range(1, num_tasks):
        start_running = QUEUED_INITIAL + n * RUN_DURATION
        complete = QUEUED_INITIAL + (n + 1) * RUN_DURATION
        if elapsed < start_running:
            return states
        if elapsed < complete:
            states.append((n, "running"))
            return states
        states.append((n, "success"))
    return states


# --- Steps accessor + templating data --------------------------------------


class _StepNamespace:
    """`steps.<node_id>` — a node's submitted field values, plus any
    node-attached @backend's return (keyed by the function name)."""

    def __init__(self, step_id: str, data: dict[str, Any]) -> None:
        self._step_id = step_id
        self._data = data

    def __getattr__(self, name: str) -> Any:
        data = self._data
        if name in data:
            return data[name]
        raise AttributeError(
            f"step {self._step_id!r} has no value {name!r}; "
            f"available: {sorted(data)}"
        )


class Steps:
    """The `steps` argument injected into @backend functions.

        steps.<node_id>.<input_id>   → a submitted field value
        steps.<backend_step_id>      → a backend step's return value

    Reading a step (or field) that hasn't run raises AttributeError, so
    typos surface immediately rather than silently returning None.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getattr__(self, name: str) -> Any:
        data = self._data
        if name not in data:
            raise AttributeError(
                f"no step {name!r} has run yet (or it doesn't exist); "
                f"available: {sorted(data)}"
            )
        value = data[name]
        if isinstance(value, dict):
            return _StepNamespace(name, value)
        return value


def build_steps_with_workflow(
    workflow: CompiledWorkflow, submission: Submission
) -> dict[str, Any]:
    """Build the `steps` data — consumed both by templating.render and
    by the Steps accessor.

      - node step      → {input_id: value, ..., backend_fn_name: return}
      - backend step   → its return value directly
    """
    steps: dict[str, Any] = {}
    for s in submission.steps:
        backend_step = workflow.by_id.get(s.node_id)
        if isinstance(backend_step, CompiledBackendStep):
            steps[s.node_id] = s.backend_return
            continue
        node = workflow.all_nodes_by_id.get(s.node_id)
        merged: dict[str, Any] = {}
        if s.form_values:
            merged.update(s.form_values)
        if (
            s.backend_return is not None
            and node is not None
            and node.backend_call
        ):
            merged[node.backend_call.fn.name] = s.backend_return
        steps[s.node_id] = merged

    # Connected Airflow operators expose their outputs — a TriggerDag's
    # run id, an XComPull's value — as top-level steps entries, so a
    # downstream `{{ steps.<operator id>.run_id }}` resolves.
    for s in submission.steps:
        for task_id, task_state in (s.external_state or {}).items():
            steps[task_id] = task_state
    return steps


def _steps_accessor(
    workflow: CompiledWorkflow, submission: Submission
) -> Steps:
    return Steps(build_steps_with_workflow(workflow, submission))


# --- Step-reference resolution ---------------------------------------------


# A template token: {{ steps.<node>.<field> }} — the same two-level
# namespace as submission_id templating and `@displays.branch`
# conditions. The regex and prop list are shared with the compiler's
# dependency collection, so they live in references.py.
from .references import STEP_REF_RE as _TEMPLATE_RE
from .references import TEMPLATED_PROPS as _TEMPLATED_PROPS


def _resolve_template_string(
    text: str,
    current_node_id: str,
    steps_data: dict[str, Any],
    *,
    is_url: bool,
) -> str:
    """Resolve `{{ steps.<node>.<field> }}` tokens in one string.

    A token naming the current node is left untouched — the browser
    resolves it live against the in-progress form. A token naming any
    other (earlier) node is replaced with that node's submitted value;
    for a `url` prop the substituted value is percent-encoded.
    """

    def sub(m: "re.Match[str]") -> str:
        node, field = m.group(1), m.group(2)
        if node == current_node_id:
            return m.group(0)  # same node — resolved live, client-side
        node_data = steps_data.get(node)
        value = node_data.get(field) if isinstance(node_data, dict) else None
        text_value = "" if value is None else str(value)
        return quote(text_value, safe="") if is_url else text_value

    return _TEMPLATE_RE.sub(sub, text)


def _resolve_prop_templates(
    props: dict[str, Any], current_node_id: str, steps_data: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """Resolve templates in a block's `label` / `url` props. Returns a
    new props dict if anything changed, else None."""
    changed: Optional[dict[str, Any]] = None
    for key in _TEMPLATED_PROPS:
        value = props.get(key)
        if not isinstance(value, str) or "{{" not in value:
            continue
        resolved = _resolve_template_string(
            value, current_node_id, steps_data, is_url=(key == "url")
        )
        if resolved != value:
            if changed is None:
                changed = dict(props)
            changed[key] = resolved
    return changed


def _resolve_step_ref(
    ref: dict[str, Any], steps_data: dict[str, Any]
) -> Any:
    """Resolve a `{node, name}` descriptor against a submission's step
    data. A field reference (`name` set) returns that one value; a
    whole-node reference (`name` is None) returns the node's entire
    value dict. Returns None when the node hasn't run, or the field
    name isn't present."""
    node_data = steps_data.get(ref.get("node"))
    if not isinstance(node_data, dict):
        return None
    name = ref.get("name")
    if name is None:
        return dict(node_data)  # whole-node reference
    return node_data.get(name)


def _eval_cross_condition(
    cond: dict[str, Any], steps_data: dict[str, Any]
) -> bool:
    """Evaluate one cross-node condition against an upstream node's
    submitted value. Mirrors the same-node evaluator in conditions.ts."""
    node_data = steps_data.get(cond.get("node"))
    actual = (
        node_data.get(cond.get("field"))
        if isinstance(node_data, dict)
        else None
    )
    op = cond.get("op")
    operand = cond.get("value")
    if op == "equals":
        return actual == operand
    if op == "not_equals":
        return actual != operand
    if op == "in":
        return isinstance(operand, list) and actual in operand
    if op == "not_in":
        return isinstance(operand, list) and actual not in operand
    if op == "contains":
        # `actual` is expected to be a list (multi-select, HITL
        # chosen_options); tolerate a scalar by treating it as a
        # one-element list.
        items = actual if isinstance(actual, list) else [actual]
        return operand in items
    if op == "truthy":
        return bool(actual)
    if op == "falsy":
        return not bool(actual)
    return True


def _resolve_block(
    block: CompiledBlock, steps_data: dict[str, Any], current_node_id: str
) -> Optional[CompiledBlock]:
    """Resolve one block. Returns None to signal the block should be
    dropped — a `When` whose cross-node conditions don't hold (its
    upstream value can't change while this node is filled, so it is
    statically hidden and removed entirely)."""
    props = block.props
    new_props: Optional[dict[str, Any]] = None

    # Cross-node conditions are resolved here, against upstream data.
    # A `When` with an unmet one is dropped; surviving blocks keep only
    # their same-node conditions, which the frontend evaluates live.
    conds = props.get("conditions")
    if isinstance(conds, list) and any(
        isinstance(c, dict) and "node" in c for c in conds
    ):
        cross = [c for c in conds if "node" in c]
        same = [c for c in conds if "node" not in c]
        if block.type == "when" and not all(
            _eval_cross_condition(c, steps_data) for c in cross
        ):
            return None
        new_props = dict(props)
        if same:
            new_props["conditions"] = same
        else:
            new_props.pop("conditions", None)

    # Upstream `options` / `default` / Sankey-column references.
    check = new_props if new_props is not None else props
    _from_keys = (
        "options_from",
        "default_from",
        "column_a_from",
        "column_b_from",
    )
    if any(k in check for k in _from_keys):
        if new_props is None:
            new_props = dict(props)
        ofrom = new_props.pop("options_from", None)
        if ofrom is not None:
            value = _resolve_step_ref(ofrom, steps_data)
            # Options must be a list; anything else degrades to empty.
            new_props["options"] = value if isinstance(value, list) else []
        dfrom = new_props.pop("default_from", None)
        if dfrom is not None:
            value = _resolve_step_ref(dfrom, steps_data)
            if value is not None:
                new_props["default"] = value
            else:
                new_props.pop("default", None)
        for col_from, col in (
            ("column_a_from", "column_a"),
            ("column_b_from", "column_b"),
        ):
            cfrom = new_props.pop(col_from, None)
            if cfrom is not None:
                value = _resolve_step_ref(cfrom, steps_data)
                new_props[col] = (
                    value if isinstance(value, list) else []
                )

    # Template resolution in string props (label, url): a token naming
    # an earlier node becomes that node's submitted value; one naming
    # the current node is left for the browser to resolve live.
    src_props = new_props if new_props is not None else props
    templated = _resolve_prop_templates(
        src_props, current_node_id, steps_data
    )
    if templated is not None:
        new_props = templated

    children: list[CompiledBlock] = []
    for child in block.children:
        resolved = _resolve_block(child, steps_data, current_node_id)
        if resolved is not None:
            children.append(resolved)

    return CompiledBlock(
        type=block.type,
        id=block.id,
        props=new_props if new_props is not None else props,
        children=children,
    )


def resolve_layout(
    workflow: CompiledWorkflow,
    submission: Submission,
    layout: CompiledBlock,
    current_node_id: str,
) -> CompiledBlock:
    """Return a copy of `layout` prepared for serving node
    `current_node_id` of this submission:

      - `options_from` / `default_from` become real `options` /
        `default` resolved against the submission's accumulated data;
      - cross-node `When` conditions are evaluated — a `When` whose
        upstream condition fails is removed, surviving ones keep only
        their same-node conditions;
      - `{{ steps.<earlier-node>.<field> }}` template tokens in `label`
        / `url` props are resolved to submitted values; tokens naming
        the current node are left for live client-side resolution.

    The compiled workflow's static layout is never mutated; the result
    carries no cross-node references, so the frontend sees an ordinary
    layout whose only remaining templates are current-node ones."""
    steps_data = build_steps_with_workflow(workflow, submission)
    resolved = _resolve_block(layout, steps_data, current_node_id)
    if resolved is None:
        # The root layout is always a container — never dropped — but
        # stay defensive.
        return CompiledBlock(type="column", id=None, props={}, children=[])
    return resolved


def resolve_template(
    workflow: CompiledWorkflow, submission: Submission, template_str: str
) -> str:
    """Convenience: render a template string against the submission's state."""
    return render(template_str, build_steps_with_workflow(workflow, submission))
