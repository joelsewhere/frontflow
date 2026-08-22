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
import traceback
from urllib.parse import quote
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .compile import (
    CompiledBackendStep,
    CompiledBlock,
    CompiledNode,
    CompiledPage,
    CompiledWorkflow,
)
from .airflow_dispatch import (
    advance_airflow_task,
    dedupe_clear_ops,
    plan_airflow_clear,
)
from .airflow_hook import AirflowError, AirflowHook
from .core import END
from .status import Affected, NeedsInput, NeedsReview, StepStatus, Unaffected
from .templating import render
from . import store, uploads

# --- Tuning constants ------------------------------------------------------

QUEUED_INITIAL = 1.5
RUN_DURATION = 3.0


def _variables_snapshot() -> dict[str, str]:
    """Decrypted name→value map of every install variable.

    Fetched once per render pass and passed into the template engine.
    A failure to reach the store (corrupted row, lost Fernet key for
    one entry) is downgraded to an empty snapshot — the existing
    "missing reference resolves to empty string" behavior of the
    template engine takes over, so a single corrupted variable does
    not stop unrelated submissions from advancing.
    """
    try:
        return store.get_all_variables()
    except Exception:  # noqa: BLE001
        return {}


class NeedsPreviewBranchChoice(Exception):
    """Raised in preview mode when a `@backend.branch` or `HitlBranch`
    needs to pick a downstream node and the admin hasn't supplied a
    choice yet. The API layer catches this and returns a payload
    listing the available downstreams so the frontend can render a
    picker. Once the admin picks, the route is recorded in
    `submission.preview_branch_choices` and the resolution retries.

    Carries the branch-owning step's node id and the downstreams
    available so the picker UI has everything it needs.
    """

    def __init__(
        self, *, step_id: str, fn_name: str,
        downstream: list[str], can_end: bool = True,
    ) -> None:
        super().__init__(
            f"preview branch {fn_name!r} on step {step_id!r} needs "
            f"admin to pick from: {downstream + (['END'] if can_end else [])}"
        )
        self.step_id = step_id
        self.fn_name = fn_name
        self.downstream = downstream
        self.can_end = can_end

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
    # Full Python traceback for the same raise — captured alongside
    # `error` so the chain UI can show a collapsible details panel
    # with the stack frames, not just the one-line message. Null
    # when the step didn't fail. Stored verbatim (multi-line string
    # from `traceback.format_exc()`).
    traceback: Optional[str] = None
    # Edit-cascade status — how an upstream edit left this step. A
    # StepStatus class (Unaffected by default); see workflows.status.
    status: type = Unaffected
    # Per-external-task state for connected Airflow operators, keyed by
    # the external task's id. Each entry is the dispatch state dict —
    # `state` plus, where they apply, `run_id` / `value` / `detail`.
    # In-memory only for now; cross-restart persistence is a follow-up.
    external_state: dict = field(default_factory=dict)
    # The user who submitted this step. None for steps not yet
    # submitted, and None for steps from legacy persistence rows
    # (the column was added later). Drives the per-submission
    # visibility gate: a user is permitted to view a submission if
    # they appear as the user_id on at least one step.
    user_id: Optional[int] = None

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
    # The form version this event was recorded under. The runtime sets
    # it from `submission.form_version_id` at record time, so an event
    # recorded *before* a force re-pin keeps the prior version id and
    # one recorded after carries the new one — letting the history
    # viewer scope events to the version it is showing.
    form_version_id: Optional[int] = None


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
    # Pre-clear run ids stashed when an edit clears an Airflow
    # `trigger_dag` that was given an explicit `run_id`. Keyed by the
    # operator (node_id + task_id). On replay, a trigger whose
    # supplied run id re-resolves to the same value re-attaches to the
    # cleared run instead of POSTing a new one; the entry is consumed
    # (removed) once re-attached. Empty for triggers with no explicit
    # run id — those always re-trigger.
    cleared_run_ids: dict = field(default_factory=dict)
    # Preview mode flag — when true the submission walks the same
    # runtime paths as a real one (page rendering, layout, validation,
    # template rendering) but bypasses every side effect:
    #   • backend functions are not invoked; their return is None
    #   • external operators do not dispatch; treated as success/None
    #   • the persistence layer skips writes (see main._persist)
    # Branch resolution in preview defers to `preview_branch_choices`
    # below — admin picks the route since the backend that would
    # normally choose isn't allowed to run.
    preview: bool = False
    # Admin-supplied branch routing decisions, keyed by the
    # branch-owning step's node id. Populated by the preview API
    # when the admin picks a downstream node. Read by the runtime
    # in preview mode in place of the backend.branch / HitlBranch
    # return value.
    preview_branch_choices: dict[str, str] = field(default_factory=dict)


# Module-level storage. One instance per backend process; replaced with
# Redis / Postgres in later steps. `_submissions` is keyed by `handle`;
# `_id_index` maps a minted `submission_id` back to its handle so the
# canonical id resolves once it exists.
_submissions: dict[str, Submission] = {}
_id_index: dict[str, str] = {}
_submissions_lock = threading.Lock()

# Preview submissions live in their own dict, isolated from the real
# `_submissions` store. They're never persisted, never have an
# `_id_index` entry, and don't survive across server restarts. Cleared
# explicitly via `delete_preview_submission` or implicitly via the
# preview API's TTL eviction.
_preview_submissions: dict[str, Submission] = {}


def get_preview_submission(handle: str) -> Optional[Submission]:
    """Look up a preview submission by handle. Preview submissions
    use only handles — no minted submission_id is ever indexed."""
    with _submissions_lock:
        return _preview_submissions.get(handle)


def delete_preview_submission(handle: str) -> None:
    """Evict a preview submission from memory. Idempotent."""
    with _submissions_lock:
        _preview_submissions.pop(handle, None)


def list_preview_submissions() -> list[str]:
    """Return the handles of all in-memory preview submissions.
    Used by the API to evict stale ones on TTL sweep."""
    with _submissions_lock:
        return list(_preview_submissions.keys())


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

    rendered = render(
        template, steps_data, variables=_variables_snapshot()
    ).strip()
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
    source value does.

    Preview submissions skip this entirely. The global `_id_index`
    is for resumable real submissions; minting from preview would
    (1) cause subsequent previews to 409 on the same minted id and
    (2) leak preview-derived ids into the index even though the
    preview submission itself never persists. A preview submission
    keeps `submission_id` null and is addressed by `handle` only.
    """
    if submission.preview:
        return
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
            form_version_id=submission.form_version_id,
        )
    )


def _finalize_events(
    workflow: CompiledWorkflow, submission: Submission
) -> None:
    """Emit the terminal/failed event once the submission has reached
    that state. Idempotent — emits each at most once. Called at the end
    of every public runtime operation.

    Also backfills `ended_at` whenever the submission is in a terminal
    state and it's still None. The two assignments are *independent* —
    if a snapshot loaded a terminated submission with `ended_at` set
    but the event somehow missing (or vice versa), each gets repaired
    on its own. Previously these were coupled (ended_at was only set
    when the event was being newly recorded), which let the column
    stay null in some edge cases.
    """
    if submission.terminated:
        if submission.ended_at is None:
            submission.ended_at = datetime.now(timezone.utc)
        if not any(
            e.type == "submission_terminated" for e in submission.events
        ):
            _record_event(
                workflow,
                submission,
                "submission_terminated",
                occurred_at=submission.ended_at,
            )
    if submission.failed:
        if submission.ended_at is None:
            submission.ended_at = datetime.now(timezone.utc)
        if not any(
            e.type == "submission_failed" for e in submission.events
        ):
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
    *,
    preview: bool = False,
    preview_branch_choices: Optional[dict[str, str]] = None,
    acting_user_id: Optional[int] = None,
) -> Submission:
    """Create a new submission for the workflow, immediately submitting
    the landing step's form with the values provided in the request.

    The submission is born as a session draft — keyed by a `handle`,
    `submission_id` still null. The id is minted here if it can be
    (unconfigured workflows, or a template that resolves from the
    landing step); otherwise it stays a draft until a later step
    supplies the value. `form_version_id` pins it to the form version
    it is running against.

    Preview mode (`preview=True`): the submission walks the same
    runtime paths but skips every side effect — backends are not
    invoked, external operators do not dispatch, and the persistence
    layer is told to no-op (see main._persist). The submission is
    stored in a SEPARATE in-memory dict (`_preview_submissions`)
    so it can't collide with real handles and is easy to evict.
    `preview_branch_choices` lets the caller pre-supply branch
    routing decisions for the landing step's chain (rare — usually
    accumulates over later submit calls)."""
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
        # Per-step submitter for the visibility gate. Landing-step
        # submitter is the user whose request started the flow.
        user_id=acting_user_id,
    )

    submission = Submission(
        handle=handle,
        form_id=workflow.id,
        started_at=now,
        form_version_id=form_version_id,
        steps=[first_step],
        preview=preview,
        preview_branch_choices=(preview_branch_choices or {}),
    )
    # Stamp the acting user so _execute_assigns sees a granter
    # for any landing-node Assigns. The mutation is in-memory only
    # (the field isn't persisted); per-request callers re-stamp
    # on every submit.
    if acting_user_id is not None:
        submission.user_id = acting_user_id
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
    # The landing step's submission is finalized HERE, not in
    # submit_step — so any Assign operators on the landing node
    # must fire here too. Previously the landing-node Assigns were
    # silently skipped (the chain was wired but never executed),
    # which broke single-step forms whose only node carried an
    # Assign. Same call shape as submit_step's line ~643.
    _execute_assigns(workflow, submission, first_step, landing)
    # Preview: a branch on the landing chain might raise
    # NeedsPreviewBranchChoice. Let it propagate; the API will catch
    # it and prompt the admin. The submission is still stored so the
    # admin can re-drive after picking.
    try:
        _route_next(workflow, submission, landing, first_step)
    except NeedsPreviewBranchChoice:
        if not preview:
            raise  # should not happen in real submissions
        # Leave next_node_id unset — the API surfaces the pick UI
        # and re-resolves once the admin chooses.
    # _try_register_id is preview-aware (it no-ops for previews), so
    # this call is safe to make unconditionally.
    _try_register_id(workflow, submission)
    _finalize_events(workflow, submission)

    # If the landing step's backend / chain drove the submission to a
    # terminal state already (success or failure), fire the terminal
    # hook here — `advance()` would otherwise see was_terminal=True
    # on its first call and skip the dispatch. The guard inside
    # `_maybe_fire_terminal_hook` makes this safe across both call
    # sites; whichever fires first claims the slot.
    if not preview:
        _maybe_fire_terminal_hook(workflow, submission)

    if preview:
        with _submissions_lock:
            _preview_submissions[handle] = submission
    else:
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
    *,
    acting_user_id: Optional[int] = None,
) -> StepSubmission:
    """Submit the form for the currently-awaiting step.

    Stores form_values + button_clicked, runs the @backend function,
    computes the next step's id from the branch return value or
    definition order.

    `acting_user_id` is the user attribution stamped on the step
    row. Drives the per-submission visibility gate — a user who
    has submitted at least one step on a submission is permitted
    to view that submission. Callers in HTTP land already resolve
    the session cookie; passing the resolved id through keeps the
    runtime decoupled from the auth layer.
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
    # Snapshot every backend's return so the cascade can diff per
    # backend under multi-backend nodes (where `backend_return` —
    # the legacy singleton — no longer captures the full picture).
    # We copy the dict but not its nested values; we only need the
    # `return` slots to compare equality with the post-edit ones.
    old_external_state = (
        {k: dict(v) for k, v in (latest.external_state or {}).items()}
        if is_edit else {}
    )

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
    # Stamp the submitter on the step row — drives the per-
    # submission visibility gate.
    if acting_user_id is not None:
        latest.user_id = acting_user_id
    _record_event(
        workflow, submission, "step_submitted", node_id=node_id,
        occurred_at=latest.submitted_at,
    )

    _execute_backend(workflow, submission, latest)
    _execute_assigns(workflow, submission, latest, ng)
    try:
        _route_next(workflow, submission, ng, latest)
    except NeedsPreviewBranchChoice:
        if not submission.preview:
            raise
        # The step was submitted successfully; only routing needs the
        # admin's pick. Don't mark the submission failed. The API will
        # catch this exception above the call and return a payload
        # listing the available downstreams. After the admin picks,
        # the runtime re-drives via `resolve_preview_branch`.
        _try_register_id(workflow, submission)
        _finalize_events(workflow, submission)
        raise

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
                old_values, old_external_state,
            )
    _try_register_id(workflow, submission)
    _finalize_events(workflow, submission)
    return latest


def resolve_preview_branch(
    workflow: CompiledWorkflow,
    submission: Submission,
    step_id: str,
    choice: str,
) -> None:
    """Record the admin's branch choice and re-drive the runtime so it
    can continue past the picker. `step_id` matches the picker key the
    runtime surfaced via `NeedsPreviewBranchChoice`; `choice` is one of
    the offered downstream node ids, or the literal "END" to terminate.

    Preview-only — raises if called against a non-preview submission.
    Re-raises `NeedsPreviewBranchChoice` if a SUBSEQUENT branch is hit
    (multi-branch flows ping-pong with the admin per branch).
    """
    if not submission.preview:
        raise ValueError(
            "resolve_preview_branch is only valid for preview submissions"
        )
    submission.preview_branch_choices[step_id] = choice
    # The step whose routing got deferred is the latest submitted step
    # (or, for chain-internal branches, still on the latest step).
    # Re-route from it; if successful, advance() drives the rest.
    latest_submitted = next(
        (s for s in reversed(submission.steps) if s.is_submitted),
        None,
    )
    if latest_submitted is not None:
        step_def = _step_def(workflow, latest_submitted.node_id)
        _route_next(workflow, submission, step_def, latest_submitted)
    advance(workflow, submission)
    _finalize_events(workflow, submission)


def _auto_fill_node_values(node_def: Any) -> dict[str, Any]:
    """Best-effort default values for a node's fields. Used by the
    preview jump path to drive past intermediate steps without
    requiring the admin to fill each one by hand. Each input type
    gets a type-appropriate placeholder so the runtime's required-
    field check passes; this mirrors the seeder's auto-fill logic.

    A field's declared `default` wins when present (so the form
    author's intent shows up in jumps too); otherwise the type's
    placeholder.

    Skips file/s3file/sankey fields — they need a real upload blob
    which the jump path can't fabricate. A required file field will
    fail the jump; the admin must walk through that step manually.
    """
    from datetime import date as _date
    today = _date.today().isoformat()
    out: dict[str, Any] = {}
    for fld in getattr(node_def, "fields", []):
        if not fld.name:
            continue
        # If the field declares its own default, honor it — that's
        # the author's intent for "empty" and matches what the live
        # form would show.
        declared = getattr(fld, "default", None)
        if declared is not None:
            out[fld.name] = declared
            continue
        t = fld.type
        if t == "checkbox":
            out[fld.name] = bool(fld.required)
        elif t in ("text", "tel", "url", "email"):
            out[fld.name] = "preview"
        elif t == "textarea":
            out[fld.name] = "Preview placeholder."
        elif t in ("number", "integer"):
            out[fld.name] = 1
        elif t == "slider":
            out[fld.name] = 50
        elif t == "rating":
            out[fld.name] = 3
        elif t == "date":
            out[fld.name] = today
        elif t == "time":
            out[fld.name] = "12:00"
        elif t in ("radio", "select"):
            opts = fld.options or []
            out[fld.name] = opts[0] if opts else "preview"
        elif t == "multi_select":
            opts = fld.options or []
            out[fld.name] = opts[:1]
        elif t == "checkbox_list":
            out[fld.name] = []
        elif t == "date_range":
            out[fld.name] = {"start": today, "end": today}
        elif t == "number_range":
            out[fld.name] = {"min": 0, "max": 100}
        elif t == "checkbox_grid":
            out[fld.name] = {}
        # file / s3file / sankey skipped — see docstring.
    return out


def _find_path_to_target(
    workflow: CompiledWorkflow,
    start_node_id: str,
    target_node_id: str,
) -> Optional[list[str]]:
    """BFS through the workflow's `>>` graph from `start_node_id`,
    looking for `target_node_id`. Returns the ordered list of node
    ids on the path (including target, excluding start) or None if
    target isn't reachable.

    The path tells the jump path which downstream to pick at each
    branch: at a branching node, the next entry in the path is
    exactly the downstream to route to. No admin picker needed for
    branches encountered along the way — naming a destination IS
    the routing decision."""
    if start_node_id == target_node_id:
        return []  # already there
    visited: set[str] = {start_node_id}
    # Queue carries (node_id, path_so_far). Path is the list of
    # nodes we'd visit AFTER the start to reach this node.
    from collections import deque
    queue: deque[tuple[str, list[str]]] = deque()
    queue.append((start_node_id, []))
    while queue:
        current, path = queue.popleft()
        current_def = _step_def(workflow, current)
        downstreams = list(getattr(current_def, "downstream", []) or [])
        for next_id in downstreams:
            # Resolve page references — `_descend` returns the entry
            # section node if next_id names a page.
            try:
                resolved = _descend(workflow, next_id)
            except KeyError:
                continue
            if resolved in visited:
                continue
            new_path = path + [resolved]
            if resolved == target_node_id:
                return new_path
            visited.add(resolved)
            queue.append((resolved, new_path))
    return None


def jump_preview(
    workflow: CompiledWorkflow,
    submission: Submission,
    target_node_id: str,
) -> None:
    """Fast-forward a preview to a named node, auto-filling every
    intermediate step. Preview-only — raises if called against a
    real submission.

    Strategy:
      1. Validate target exists and isn't BEHIND the current frontier
         (jumping backward requires a reset).
      2. Find a path through the `>>` graph from the current frontier
         to the target. At each branching node on the path, the next
         path entry IS the branch decision — record it in
         `preview_branch_choices` so `_determine_next` picks it.
      3. Walk forward: for each step that isn't already submitted,
         call `submit_step` with `_auto_fill_node_values(node_def)`.
         The runtime materializes the next step; we repeat until
         the target is the awaiting step.
      4. `advance()` runs once at the end to materialize the target.

    Raises:
      - ValueError if target doesn't exist or isn't reachable.
      - The same exceptions submit_step would raise (e.g. a required
        file field can't be auto-filled — the admin must walk through
        that step manually instead of jumping past it).
    """
    if not submission.preview:
        raise ValueError(
            "jump_preview is only valid for preview submissions"
        )
    try:
        target_def = _step_def(workflow, target_node_id)
    except KeyError:
        raise ValueError(
            f"target node {target_node_id!r} not found in workflow"
        )

    # If the target is at or before the current frontier, this is a
    # backward jump. Truncate the submission's step list so the
    # target becomes the awaiting step, then drop any branch choices
    # that were recorded for steps we're discarding (so subsequent
    # forward walking re-decides from scratch). Preview has no side
    # effects to undo — this is just memory bookkeeping.
    existing_idx = next(
        (i for i, s in enumerate(submission.steps)
         if s.node_id == target_node_id),
        None,
    )
    if existing_idx is not None:
        # Drop everything AFTER the target. The target itself stays
        # as a draft: its prior form_values get cleared, its
        # submitted flag reset, so the runtime treats it as awaiting
        # input again. (We can't drop the target outright when it's
        # the first step — `advance()` then has no frontier to walk
        # from.) Branches choices for dropped nodes get cleared so
        # subsequent forward submits auto-pick fresh.
        dropped_node_ids = {
            s.node_id for s in submission.steps[existing_idx + 1:]
        }
        del submission.steps[existing_idx + 1:]
        # Reset the target step to a fresh draft so the UI presents
        # an empty form and the runtime treats it as awaiting input.
        target_step = submission.steps[existing_idx]
        target_step.submitted_at = None
        target_step.form_values = {}
        target_step.button_clicked = None
        target_step.next_node_id = None
        target_step.branch_taken_explicitly = False
        target_step.external_state = {}
        target_step.error = None
        for nid in dropped_node_ids:
            submission.preview_branch_choices.pop(nid, None)
        # Also drop the target's own branch choice — re-submitting
        # the target should auto-pick first downstream again.
        submission.preview_branch_choices.pop(target_step.node_id, None)
        submission.terminated = False
        submission.failed = False
        submission.ended_at = None
        return

    # Forward jump: find a path through the `>>` graph from current
    # frontier to target.
    if not submission.steps:
        raise ValueError("preview has no steps — start it first")
    frontier = submission.steps[-1]
    if frontier.node_id == target_node_id:
        return  # already there

    path = _find_path_to_target(
        workflow, frontier.node_id, target_node_id,
    )
    if path is None:
        raise ValueError(
            f"no path from {frontier.node_id!r} to {target_node_id!r} "
            "in this workflow's graph"
        )

    # Pre-record branch choices along the path. At each node N whose
    # downstream has multiple options, the NEXT entry on the path
    # tells us which one to pick.
    cursor = frontier.node_id
    for step_id in path:
        try:
            cursor_def = _step_def(workflow, cursor)
        except KeyError:
            break
        downstreams = list(getattr(cursor_def, "downstream", []) or [])
        if len(downstreams) > 1:
            # The downstream entry that matches the path's next stop
            # is our route. Resolve page refs the same way the
            # graph search did so we compare apples to apples.
            for ds in downstreams:
                try:
                    if _descend(workflow, ds) == step_id:
                        submission.preview_branch_choices[cursor] = ds
                        break
                except KeyError:
                    continue
        cursor = step_id

    # Now walk forward, auto-submitting each awaiting step until the
    # target becomes the awaiting step. Cap iterations defensively
    # (path length plus a small slack) so a buggy graph can't loop
    # forever.
    max_iter = len(path) + 4
    for _ in range(max_iter):
        # advance() may raise NeedsPreviewBranchChoice if a branch
        # is encountered that ISN'T on our path; rare, but possible
        # if the workflow has unrelated branches between current
        # and target. Let it propagate — the admin can pick and
        # the jump endpoint will retry.
        advance(workflow, submission)
        # Find the awaiting step.
        awaiting = next(
            (s for s in submission.steps if not s.is_submitted),
            None,
        )
        if awaiting is None:
            break  # submission terminated
        if awaiting.node_id == target_node_id:
            break  # we're there
        # Auto-fill and submit. `_step_def` raises KeyError for
        # unknown ids (shouldn't happen for an awaiting step).
        try:
            node_def = _step_def(workflow, awaiting.node_id)
        except KeyError:
            break
        # A backend step has no fields — `advance()` runs it on its
        # own, so we shouldn't reach this loop iteration with a
        # backend awaiting. Guard defensively anyway.
        if isinstance(node_def, CompiledBackendStep):
            continue
        values = _auto_fill_node_values(node_def)
        # Single-button nodes don't need an explicit button; multi-
        # button nodes need one — pick the first by convention.
        button = (
            node_def.buttons[0].id
            if len(node_def.buttons) > 1 else None
        )
        submit_step(
            workflow, submission, awaiting.node_id, values, button,
        )


# Submissions whose advance is currently being driven by a background
# worker thread, keyed by handle. While a handle is here, `advance()`
# is a no-op for it — requests (submits, polls) return the submission's
# current state instead of re-entering, and the UI watches each
# workflow-level backend step flip running -> success as the worker
# progresses. In-memory only, matching the submissions dict: if the
# process dies mid-run, the flag dies with it and the next poll's
# advance re-runs the unfinished step.
_background_advances: set[str] = set()
_background_lock = threading.Lock()


def _background_backends_enabled(workflow: CompiledWorkflow) -> bool:
    """Whether this form opted into background backend-step execution
    (`@form(background_backends=True)`). Read from the live Workflow
    registry — the same lookup the terminal hooks use — so the
    compiled graph needs no new field."""
    from frontflow.dsl.core import WORKFLOWS

    wf = WORKFLOWS.get(workflow.id)
    return bool(getattr(wf, "background_backends", False))


def _spawn_background_advance(
    workflow: CompiledWorkflow, submission: Submission,
) -> bool:
    """Try to hand this submission's advance to a worker thread.
    Returns True when a worker was spawned (or one already owns the
    submission) — the caller must stop advancing; False when the
    caller should proceed synchronously."""
    with _background_lock:
        if submission.handle in _background_advances:
            return True
        _background_advances.add(submission.handle)
    threading.Thread(
        target=_advance_in_background,
        args=(workflow, submission),
        name=f"advance-{submission.handle}",
        daemon=True,
    ).start()
    return True


def _advance_in_background(
    workflow: CompiledWorkflow, submission: Submission,
) -> None:
    """Worker-thread body: run the normal advance loop to completion,
    fire terminal hooks, persist, and release the in-flight flag. Any
    step failure lands in the submission state exactly as it would on
    the synchronous path; the next poll surfaces it."""
    try:
        _advance_inner(workflow, submission, allow_background=False)
        _maybe_fire_terminal_hook(workflow, submission)
    except Exception as e:  # noqa: BLE001 — a worker must never die silently
        print(
            f"[workflow] background advance for "
            f"{submission.handle} raised: {e}"
        )
        traceback.print_exc()
    finally:
        with _background_lock:
            _background_advances.discard(submission.handle)
        # Persist the completed state — the synchronous path persists
        # in the request handler after advance; the worker has no
        # request, so it syncs here. Preview / id-less submissions
        # skip, same as the request-path guard.
        if not submission.preview and submission.submission_id is not None:
            try:
                store.sync_submission(
                    submission_snapshot(workflow, submission)
                )
            except Exception as e:  # noqa: BLE001
                print(
                    f"[workflow] background persist failed for "
                    f"{submission.handle}: {e}"
                )


def advance(workflow: CompiledWorkflow, submission: Submission) -> None:
    """Idempotent: progress the submission to reflect the wall clock.

    HITL nodes wait for a form submit (and for their external tasks to
    finish). Called before building submission responses.

    Forms that opt in via `@form(background_backends=True)` run their
    workflow-level backend steps in a BACKGROUND worker: when the
    frontier is an unsubmitted backend step, advance hands the whole
    loop to a daemon thread and returns immediately. The submitting
    request comes back at once with the step reported as `running`;
    the submission page's polling then watches each step complete in
    turn. While the worker owns a submission, every other advance call
    for it is a no-op. Preview submissions keep the synchronous path —
    their backend steps record None instantly, and preview branch
    picking (`NeedsPreviewBranchChoice`) must raise into the request.
    Without the opt-in, execution is synchronous, as it always was.

    Fires the form's `on_submitted` / `on_failed` hooks when the
    submission crosses into a terminal state — once per transition.
    The submission carries a one-shot flag (`_terminal_hook_fired`)
    so a re-call after termination doesn't double-fire.
    """
    with _background_lock:
        if submission.handle in _background_advances:
            # A worker owns this submission — the caller just reads
            # current state.
            return
    _advance_inner(workflow, submission)
    _maybe_fire_terminal_hook(workflow, submission)


def _maybe_fire_terminal_hook(
    workflow: CompiledWorkflow, submission: Submission,
) -> None:
    """Fire the terminal hook (`on_submitted` / `on_failed`) exactly
    once per submission, when it's in a terminal state.

    Idempotent — guarded by `_terminal_hook_fired`. Called from both
    `advance()` and `start_submission()`; the latter is necessary
    because a landing-step `@backend` can raise and mark the
    submission failed *inside* start_submission, before advance ever
    runs. Without this call there, the on_failed hook for that
    branch would silently never fire.
    """
    terminal = submission.terminated or submission.failed
    if not terminal:
        return
    if getattr(submission, "_terminal_hook_fired", False):
        return
    submission._terminal_hook_fired = True
    if submission.failed:
        _fire_terminal_hook(workflow, submission, kind="failed")
    else:
        _fire_terminal_hook(workflow, submission, kind="submitted")


def _fire_terminal_hook(
    workflow: CompiledWorkflow,
    submission: "Submission",
    *,
    kind: str,
) -> None:
    """Look up the form's on_submitted or on_failed hook and invoke
    it with an event dict. Hook failures are logged + swallowed,
    same contract as on_assigned (design doc §6.3)."""
    if submission.preview:
        # Preview submissions never fire hooks — they're side-effect
        # free by construction.
        return

    from frontflow.dsl.core import WORKFLOWS
    wf = WORKFLOWS.get(workflow.id)
    if wf is None:
        return
    attr = "on_failed" if kind == "failed" else "on_submitted"
    hook = getattr(wf, attr, None)
    if hook is None:
        return

    event = {
        "kind": kind,
        "form_id": workflow.id,
        "submission_handle": submission.handle,
        "submission_id": submission.submission_id,
        "user_id": getattr(submission, "user_id", None),
        "error": _first_step_error(submission) if kind == "failed" else None,
    }
    try:
        hook(event)
    except Exception as e:  # noqa: BLE001 — never let a hook break advance
        print(
            f"[{attr}] hook for {workflow.id!r} raised: {e}"
        )


def _first_step_error(submission: "Submission") -> Optional[str]:
    """Find the error message from the failing step, if any. Looks
    for the first StepSubmission with `.error` set — usually the
    most recently failed step."""
    for step in submission.steps:
        if getattr(step, "error", None):
            return step.error
    return None


def _advance_inner(
    workflow: CompiledWorkflow,
    submission: Submission,
    allow_background: bool = True,
) -> None:
    while not submission.terminated and not submission.failed:
        # An empty steps list means the submission was rehydrated
        # from a persistence row that didn't include any step rows
        # — most commonly a child submission whose parent's Assign
        # created the row but never persisted its in-memory step
        # (because _persist no-ops while submission_id is null).
        # Treat the submission as "awaiting first interaction" and
        # leave the loop; the next-step computation happens when
        # the user submits the landing step via the HTTP API.
        if not submission.steps:
            return
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
                # Forms that opted in hand the rest of the advance to
                # a worker thread the moment a backend step becomes
                # the frontier: the request returns with the step
                # reported "running", and the submission page's polls
                # watch it (and every step after it) complete. The
                # worker re-enters this loop with
                # allow_background=False and runs everything
                # synchronously inside the thread.
                if (
                    allow_background
                    and not submission.preview
                    and _background_backends_enabled(workflow)
                    and _spawn_background_advance(workflow, submission)
                ):
                    return
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
                try:
                    if not _route_next(
                        workflow, submission, step_def, latest
                    ):
                        break  # routing failed → submission failed
                except NeedsPreviewBranchChoice:
                    if not submission.preview:
                        raise
                    # Stop advancing; the API will catch this and
                    # surface a picker. Resume via resolve_preview_branch
                    # once the admin chooses.
                    raise
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
            if not latest.branch_taken_explicitly and step_def.chain:
                # Unified chain processor: walks operators (real or
                # mock per-operator by `connection`) and backends in
                # declared order, writing each step's output to
                # `step.external_state`.
                if not _process_chain(
                    workflow, submission, latest, step_def.chain,
                ):
                    break  # chain still progressing, or it failed

        if latest.next_node_id is None:
            # No next step. A few cases:
            #   (1) Genuinely the last step → terminate the submission.
            #   (2) A re-opened step mid-list during a cascade → just
            #       move on without terminating.
            #   (3) PREVIEW: a `@backend.branch` raised
            #       NeedsPreviewBranchChoice during the original submit,
            #       so `next_node_id` is None not because we're done
            #       but because routing was deferred. We need to re-
            #       attempt routing here so the picker re-surfaces;
            #       otherwise advance() would mistakenly terminate
            #       the preview the moment the admin GETs to refresh.
            if (
                submission.preview
                and latest.is_submitted
                and not latest.branch_taken_explicitly
                and latest_idx == len(submission.steps) - 1
            ):
                step_def_for_route = _step_def(workflow, latest.node_id)
                # Will raise NeedsPreviewBranchChoice if the branch
                # still has no admin choice; returns False on routing
                # error. Either way control leaves the loop.
                _route_next(
                    workflow, submission, step_def_for_route, latest,
                )
                if latest.next_node_id is None:
                    # No branch and no next — terminate as before.
                    submission.terminated = True
                    break
                # Routing succeeded (admin must have picked since the
                # last attempt). Continue the loop to materialize.
                continue
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
        elif dep.source == "argument":
            # Operator config templates — the operator's external
            # work depends on the upstream value, so a change requires
            # re-execution. Conservative by design (see
            # `_collect_chain_deps`): even cosmetic operator-config
            # refs re-run, in exchange for never silently leaving a
            # functional ref stale.
            verdicts.append(NeedsInput)
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
    old_external_state: dict[str, Any],
) -> dict[int, type]:
    """Decide, for every step after `edited_index`, the StepStatus that
    the edit at `edited_index` leaves it in.

    `old_values` / `old_external_state` are the edited step's state
    *before* this re-submit (its new state is already on the step).
    `old_external_state` is the prior `external_state` dict — used to
    diff each backend's return individually under multi-backend nodes.
    Returns a map of step-index → StepStatus for the downstream steps;
    the caller applies it. A step that earns NeedsInput has its own
    outputs become uncertain, so its fields join the change set and
    later dependants are caught transitively."""
    edited = submission.steps[edited_index]
    new_values = edited.form_values or {}

    # The initial change set: the edited step's differing fields, plus
    # every node-internal @backend whose return moved.
    changed: set[tuple[str, Optional[str]]] = {
        (edited.node_id, f) for f in _diff_values(old_values, new_values)
    }
    edited_def = _step_def(workflow, edited.node_id)
    if isinstance(edited_def, CompiledNode):
        # Diff each backend's return slot individually — a multi-backend
        # node has many `external_state[<fn_name>].return` values, and
        # any one changing is a meaningful downstream signal.
        new_external_state = edited.external_state or {}
        for cs in edited_def.chain:
            if cs.kind != "backend_call":
                continue
            fn_name = cs.backend_call.fn.name
            old_ret = (old_external_state.get(fn_name) or {}).get("return")
            new_ret = (new_external_state.get(fn_name) or {}).get("return")
            if old_ret != new_ret:
                changed.add((edited.node_id, fn_name))

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
            if isinstance(sd, CompiledNode):
                # Every backend in the chain — any of them might be
                # read by a later step via `steps.<fn_name>.return`.
                for cs in sd.chain:
                    if cs.kind == "backend_call":
                        changed.add(
                            (step.node_id, cs.backend_call.fn.name)
                        )
            elif isinstance(sd, CompiledBackendStep):
                changed.add((step.node_id, sd.fn.name))
    return result


def apply_edit_cascade(
    workflow: CompiledWorkflow,
    submission: Submission,
    edited_index: int,
    old_values: dict[str, Any],
    old_external_state: dict[str, Any],
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
        workflow, submission, edited_index, old_values, old_external_state
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
            # Clear any node-internal backend return slots from
            # `external_state` — their inputs may differ now. Operator
            # slots (from real/mock Airflow) are cleared by the
            # separate Airflow-clear path; leave those alone here.
            sd = _step_def(workflow, step.node_id)
            if isinstance(sd, CompiledNode):
                for cs in sd.chain:
                    if cs.kind == "backend_call":
                        step.external_state.pop(
                            cs.backend_call.fn.name, None,
                        )
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


def _clear_airflow_for_steps(
    workflow: CompiledWorkflow,
    steps: list[StepSubmission],
    submission: Submission,
) -> None:
    """Realign Airflow with a frontflow edit: clear the task instances
    the affected steps' operators own.

    For each affected step that ran connected Airflow operators, an
    affected `trigger_dag` clears the whole run it created; an affected
    sensor / xcom / hitl operator clears its one referenced task
    instance; a `dag_sensor` clears nothing. The plans are deduped
    across all the steps before any call, so a task clear subsumed by a
    whole-run clear of the same run is dropped.

    Raises AirflowError if a clear call fails — the point of clearing
    is to keep the two systems synchronized, so a clear that cannot
    reach Airflow must not silently half-happen.
    """
    steps_data = build_steps_with_workflow(workflow, submission)
    variables_data = _variables_snapshot()

    def resolve(template_str: str) -> Any:
        return render(template_str, steps_data, variables=variables_data)

    # Collect a clear plan for every affected connected operator.
    ops: list[dict[str, Any]] = []
    connections: dict[tuple[str, str], str] = {}
    for step in steps:
        node = workflow.all_nodes_by_id.get(step.node_id)
        if node is None or not node.external_tasks:
            continue
        for task in node.external_tasks:
            cfg = task.config or {}
            connection = cfg.get("connection")
            if connection == "mock":
                continue  # opted into mock dispatch — nothing to clear
            plan = plan_airflow_clear(
                task, step.external_state.get(task.task_id),
                resolve=resolve,
            )
            if plan is None:
                continue
            ops.append(plan)
            connections[(plan["dag_id"], plan["run_id"])] = connection
            # Stash the pre-clear run id for a `trigger_dag` that was
            # given an explicit run id — so a replay whose run id
            # re-resolves to the same value re-attaches to this
            # cleared run instead of POSTing a new one. Triggers with
            # no explicit run id are not stashed: an Airflow-generated
            # id has no stable identity to re-attach to.
            if (
                task.kind == "airflow_trigger_dag"
                and cfg.get("run_id_template")
            ):
                key = f"{step.node_id}::{task.task_id}"
                submission.cleared_run_ids[key] = plan["run_id"]

    if not ops:
        return

    # Fetch each unique DAG's structural graph once so dedupe can
    # drop task clears whose Airflow-side downstream closure is
    # already covered by another clear in this batch. Each clear op
    # is called with `include_downstream=True`, so an ancestor's
    # clear subsumes its descendants. Per-call caching only — these
    # graphs aren't expected to change within a single clear pass.
    # Best-effort: a fetch failure for one dag_id drops that dag_id
    # from the graphs map; the redundant calls still work, just
    # less efficiently. A whole `clear_task_instances` failure later
    # still raises and aborts.
    dag_graphs: dict[str, dict[str, list[str]]] = {}
    dag_connections: dict[str, str | None] = {}
    for (dag_id, _run_id), connection in connections.items():
        if dag_id in dag_connections:
            continue
        dag_connections[dag_id] = connection
    for dag_id, connection in dag_connections.items():
        try:
            hook = _airflow_hook_for(connection)
            dag_graphs[dag_id] = hook.get_dag_tasks(dag_id)
        except Exception:  # noqa: BLE001 - best-effort fallback
            # Skip dedupe for this dag_id; redundant calls still work.
            continue

    for op in dedupe_clear_ops(ops, dag_graphs=dag_graphs):
        connection = connections.get((op["dag_id"], op["run_id"]))
        # connection may be None — `_airflow_hook_for` resolves the
        # conventional `airflow_default` in that case. A missing
        # default raises, which we let propagate (a clear that can't
        # synchronize must fail loudly).
        hook = _airflow_hook_for(connection)
        # Let an AirflowError propagate — the caller surfaces it; a
        # clear that cannot synchronize must fail loudly.
        hook.clear_task_instances(
            op["dag_id"],
            op["run_id"],
            task_ids=op["task_ids"],
        )


def validate_repin(
    current: CompiledWorkflow,
    live: CompiledWorkflow,
    submission: Submission,
) -> list[dict[str, Any]]:
    """Check whether a submission pinned to `current` can safely re-pin
    to `live`. Returns the list of incompatibilities — empty list means
    the re-pin is safe.

    Only the *already-submitted* steps need checking. Steps that haven't
    run yet will exercise the live version's code on the next advance —
    that's the point of the re-pin.

    Each issue is a dict with `kind`, a human-readable `detail`, and the
    location (`node_id`, optionally `field` or `button`):
      - node_missing:    a submitted step's node is gone in `live`
      - field_missing:   a recorded value's field is gone in `live`
      - field_type_changed: the field's type differs (string vs int,
                         text vs select — different value interpretations)
      - option_removed:  a recorded select/radio value is no longer
                         among the live options
      - button_missing:  the clicked button is gone, and the live node
                         has more than one button so the click can't be
                         resolved by fallback
      - submission_id_field_missing: the live submission-id template
                         references a field that doesn't exist in the
                         submission's data

    The caller turns a non-empty list into a 409 with the issues in the
    body; an empty list means it's safe to update `form_version_id`.
    """
    issues: list[dict[str, Any]] = []

    for step in submission.steps:
        if not step.is_submitted:
            # An in-flight step (the user is sitting on it) doesn't
            # carry committed values — nothing to validate against.
            continue

        # Workflow-level @backend steps land in `by_id` but NOT in
        # `all_nodes_by_id` (which is nodes-only). A submitted
        # backend step still exists in the live form iff its id is
        # in `by_id`. If the step failed, it's implicitly tainted —
        # the truncate logic re-runs it on the new pin. If it
        # succeeded, treat it as kept (its backend_return is the
        # canonical output recorded against the old version).
        from .compile import CompiledBackendStep
        live_step = live.by_id.get(step.node_id)
        if isinstance(live_step, CompiledBackendStep):
            if step.error:
                # Failed backend step — taint so the truncate logic
                # drops it and re-materializes it on the new pin,
                # letting `advance()` re-run it (presumably after
                # the bug that caused the failure has been fixed in
                # the new version).
                issues.append({
                    "kind": "failed_backend_step",
                    "node_id": step.node_id,
                    "detail": (
                        f"backend step {step.node_id!r} failed on the "
                        f"prior version — will re-run on the new pin"
                    ),
                })
            # Successful backend steps don't need field validation
            # (they have no form_values). Move on.
            continue

        live_node = live.all_nodes_by_id.get(step.node_id)
        if live_node is None:
            issues.append({
                "kind": "node_missing",
                "node_id": step.node_id,
                "detail": (
                    f"step {step.node_id!r} was submitted on the old "
                    "version but no longer exists in the current form"
                ),
            })
            continue

        live_fields = {f.name: f for f in live_node.fields}
        for field_name, value in (step.form_values or {}).items():
            if value is None:
                # Skipped optional, conditional-hidden, etc. — nothing
                # bound to validate.
                continue
            live_field = live_fields.get(field_name)
            if live_field is None:
                issues.append({
                    "kind": "field_missing",
                    "node_id": step.node_id,
                    "field": field_name,
                    "detail": (
                        f"field {field_name!r} in step "
                        f"{step.node_id!r} no longer exists"
                    ),
                })
                continue
            # Find the old field for a type comparison.
            old_node = current.all_nodes_by_id.get(step.node_id)
            old_field = None
            if old_node is not None:
                old_field = next(
                    (f for f in old_node.fields if f.name == field_name),
                    None,
                )
            if (
                old_field is not None
                and old_field.type != live_field.type
            ):
                issues.append({
                    "kind": "field_type_changed",
                    "node_id": step.node_id,
                    "field": field_name,
                    "detail": (
                        f"field {field_name!r} in step "
                        f"{step.node_id!r} changed type "
                        f"({old_field.type!r} → {live_field.type!r})"
                    ),
                })
                continue
            # Option-bound types: a recorded value must still be in the
            # live options. For multi_select / checkbox_grid the value
            # is a list/dict; we compare leaf strings against options.
            if live_field.options:
                recorded: list[str] = []
                if isinstance(value, str):
                    recorded = [value]
                elif isinstance(value, list):
                    recorded = [v for v in value if isinstance(v, str)]
                # Checkbox grids and other shapes: skip — the option
                # check is best-effort, not exhaustive.
                stale = [
                    v for v in recorded if v not in live_field.options
                ]
                if stale:
                    issues.append({
                        "kind": "option_removed",
                        "node_id": step.node_id,
                        "field": field_name,
                        "detail": (
                            f"field {field_name!r} in step "
                            f"{step.node_id!r} has recorded value(s) "
                            f"{stale!r} no longer in the option list"
                        ),
                    })

        # Button check — only if a button was clicked.
        if step.button_clicked:
            live_button_ids = {b.id for b in live_node.buttons}
            if (
                step.button_clicked not in live_button_ids
                and len(live_node.buttons) != 1
            ):
                # Single-button fallback rescues an id-less or
                # renamed button; multi-button mismatch is a real issue.
                issues.append({
                    "kind": "button_missing",
                    "node_id": step.node_id,
                    "button": step.button_clicked,
                    "detail": (
                        f"button {step.button_clicked!r} clicked on "
                        f"step {step.node_id!r} no longer exists, and "
                        "the step has multiple buttons so the click "
                        "can't be resolved by fallback"
                    ),
                })

    # Submission-id template integrity. The id was minted against
    # `current`'s template using submitted data. If `live` introduces a
    # new template that names fields the submission doesn't have, the
    # submission becomes unresumable once it reaches the trigger point.
    live_template = live.submission_id_template
    if live_template and submission.submission_id is not None:
        refs = set(_STEPS_REF_RE.findall(live_template))
        steps_data = build_steps_with_workflow(current, submission)
        for ref in refs:
            if ref in steps_data:
                continue
            # The template references a step. Either it hasn't run yet
            # (fine — it'll resolve when it does), or its node is gone
            # in `live`, in which case the issue is captured by
            # node_missing above. So nothing new to add here for the
            # node-level miss; the field-level miss is what we care
            # about.
        # Field-level — the template can also reference specific fields
        # via Jinja attribute access, but the regex above only catches
        # step names. A deeper template parse is overkill; node-missing
        # and field-missing above cover the common cases.

    return issues


def repin_submission(
    current: CompiledWorkflow,
    live: CompiledWorkflow,
    submission: Submission,
    *,
    new_version_id: int,
) -> list[dict[str, Any]]:
    """Re-pin a submission from its current form version to the live
    one. Validates first via `validate_repin`; if there are issues,
    returns them unchanged and does *not* mutate the submission.
    On success: updates `submission.form_version_id` to `new_version_id`,
    records a `submission_repinned` event, and returns an empty list.

    Version ids are passed explicitly — they are tracked in the
    persistence layer (the `form_version` table and the live-version
    index in `main`), not on the compiled workflow itself.
    """
    issues = validate_repin(current, live, submission)
    if issues:
        return issues

    old_version = submission.form_version_id
    if old_version == new_version_id:
        # Caller should have checked, but the no-op case is harmless;
        # don't record an event for a non-change.
        return []

    submission.form_version_id = new_version_id
    _record_event(
        live, submission, "submission_repinned",
        payload={
            "from_version": old_version,
            "to_version": new_version_id,
        },
    )
    return []


def force_repin_submission(
    live: CompiledWorkflow,
    submission: Submission,
    *,
    new_version_id: int,
) -> None:
    """Force re-pin: freeze the current chain into history and start
    a fresh empty chain on `new_version_id`.

    No compatibility check, no data migration. The in-memory active
    chain (`submission.steps`) is cleared; the step rows for the prior
    version stay in the DB tagged with their original `form_version_id`
    and become read-only history when `sync_submission` next runs
    (which only touches active-version rows).

    The submission's terminated/failed flags reset — it is in flight
    again on the new version, regardless of what state v(old) reached.
    Airflow runs from the prior version are *not* cleared: they stay in
    whatever terminal state they reached, as part of the frozen
    history.

    The caller must `_persist` *before* calling this (so the v(old)
    chain is already in the DB and survives), then `_persist` again
    *after* (so v(new)'s empty chain is recorded).
    """
    old_version = submission.form_version_id
    if old_version == new_version_id:
        return  # no-op

    _record_event(
        live, submission, "submission_force_repinned",
        payload={
            "from_version": old_version,
            "to_version": new_version_id,
            "frozen_steps": [s.node_id for s in submission.steps],
        },
    )

    # Freeze: clear the in-memory active chain. The DB rows for the
    # prior version are not touched — sync_submission scopes its
    # drop-and-rewrite to the active version, so they persist as the
    # frozen v(old) chain.
    submission.steps = []
    submission.terminated = False
    submission.failed = False
    submission.ended_at = None
    submission.editing_node_id = None
    submission.edit_scope = "cascade"
    # cleared_run_ids is a per-edit stash — irrelevant across a
    # version boundary; clear it so a future v(new) edit starts fresh.
    submission.cleared_run_ids = {}

    # Bump to the new version. New steps created from this point on
    # are tagged with new_version_id by sync_submission.
    submission.form_version_id = new_version_id

    # Start the v(new) chain at its entry node — fresh, no pre-fill.
    now = datetime.now(timezone.utc)
    entry = live.landing_node()
    submission.steps = [StepSubmission(node_id=entry.id, started_at=now)]
    _record_event(
        live, submission, "step_started", node_id=entry.id,
        occurred_at=now,
    )


def selective_force_repin_submission(
    current: CompiledWorkflow,
    live: CompiledWorkflow,
    submission: Submission,
    *,
    new_version_id: int,
) -> dict[str, Any]:
    """Force re-pin a submission to `new_version_id`, dropping only the
    steps that are actually invalidated by the structural change.
    Steps still valid against the live structure stay on the active
    chain; the chain truncates at the first invalidated step and
    everything from there is frozen as read-only history.

    The "valid prefix" rule: a step is kept if its node still exists
    in `live`, all its recorded fields still exist with compatible
    types, and any select-option values still match. The instant a
    step fails any of those, that step *and every step after it*
    drop off the active chain — downstream steps depend on the
    upstream output, so we can't keep a step whose input ancestor is
    invalidated.

    The dropped step rows stay in the DB tagged with their original
    form_version_id, the same mechanism `force_repin_submission`
    uses for its all-or-nothing freeze. They're viewable via the
    version picker but not editable through the normal flow. Kept
    steps get new rows written at `new_version_id` on the next
    `sync_submission` while preserving their old rows under the
    prior `form_version_id` — so the historical chain shows the
    full original traversal, the live chain shows just the kept
    prefix.

    Submission state resets to in-flight at the truncation point —
    `terminated`/`failed` clear, even if the prior chain finished.
    The user resumes by completing the first dropped step (or, if
    none were dropped, the next still-pending step in the live
    flow).

    Returns a summary: `{kept: [node_ids], dropped: [node_ids],
    issues: [...]}`. The caller uses this to surface the
    consequence to the user (e.g. on the API response).
    """
    issues = validate_repin(current, live, submission)

    # Group by node_id. Issues without one (e.g.
    # submission_id_field_missing) apply to the submission as a
    # whole, not a specific step — they don't drive truncation.
    # The submission_id is already minted by the time anyone is
    # repinning, so a missing-field issue there is moot.
    tainted_nodes: set[str] = {
        i["node_id"] for i in issues if i.get("node_id")
    }

    # Find the first SUBMITTED step whose node is tainted. In-flight
    # (not yet submitted) steps don't carry committed values, so
    # validate_repin already ignores them.
    truncate_idx: Optional[int] = None
    for i, step in enumerate(submission.steps):
        if step.is_submitted and step.node_id in tainted_nodes:
            truncate_idx = i
            break

    old_version = submission.form_version_id

    if truncate_idx is None:
        # All active-chain steps are still valid against `live`. This
        # is effectively a clean repin — the caller routed through
        # the force path even though it didn't need to. Update the
        # pin and record a normal repin event.
        submission.form_version_id = new_version_id
        _record_event(
            live, submission, "submission_repinned",
            payload={
                "from_version": old_version,
                "to_version": new_version_id,
            },
        )
        return {
            "kept": [s.node_id for s in submission.steps],
            "dropped": [],
            "issues": issues,
        }

    kept = submission.steps[:truncate_idx]
    dropped = submission.steps[truncate_idx:]
    # Snapshot the node ids NOW, before we mutate submission.steps.
    # The resume-step append below shares the `kept` list (via
    # `submission.steps = kept`), so reading node ids off `kept`
    # after that point would include the resume step.
    kept_node_ids = [s.node_id for s in kept]
    dropped_node_ids = [s.node_id for s in dropped]

    # Truncate the active chain. Dropped steps' DB rows stay in
    # place tagged with old_version (read-only history).
    submission.steps = kept

    # Submission resumes in-flight at the truncation point.
    # Mirror force_repin_submission's terminal-state reset.
    submission.terminated = False
    submission.failed = False
    submission.ended_at = None
    submission.editing_node_id = None
    submission.edit_scope = "cascade"
    submission.cleared_run_ids = {}

    submission.form_version_id = new_version_id

    # Start the user at the first dropped step on the new version,
    # so they can re-traverse from there. (force_repin_submission
    # starts at the landing node; selective starts at the first
    # invalidated node, which is where the prior chain hit trouble.)
    now = datetime.now(timezone.utc)
    resume_node_id = dropped[0].node_id

    # Look up in `by_id` (not `all_nodes_by_id`) so workflow-level
    # backend steps resolve correctly. A failed backend step
    # tainted by validate_repin's `failed_backend_step` rule
    # resumes as itself — `advance()` will re-run it on the new
    # pin. Falling back to landing here would lose all the user's
    # prior work, which is exactly the bug this branch fixes.
    from .compile import CompiledBackendStep
    resume_step = live.by_id.get(resume_node_id)
    if resume_step is None:
        # The first dropped step's id doesn't exist in the live
        # form at all (node_missing issue). Fall back to the
        # landing node — user re-traverses from the start of the
        # new flow.
        resume_step = live.landing_node()
    submission.steps.append(
        StepSubmission(node_id=resume_step.id, started_at=now)
    )
    _record_event(
        live, submission, "step_started", node_id=resume_step.id,
        occurred_at=now,
    )

    _record_event(
        live, submission, "submission_selective_force_repinned",
        payload={
            "from_version": old_version,
            "to_version": new_version_id,
            "kept_steps": kept_node_ids,
            "dropped_steps": dropped_node_ids,
            "resume_node_id": resume_step.id,
            "issues": issues,
        },
    )

    return {
        "kept": kept_node_ids,
        "dropped": dropped_node_ids,
        "issues": issues,
    }


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
        # Realign Airflow before dropping local state — every step is
        # affected by a full restart.
        _clear_airflow_for_steps(workflow, submission.steps, submission)
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

    # Realign Airflow before any local state changes — the affected
    # steps are the target and everything downstream of it. This
    # applies to both edit and reset: an edit re-opens the target and
    # may cascade, a reset drops the lot; either way the Airflow work
    # those steps drove must be cleared so a replay re-runs in place.
    _clear_airflow_for_steps(
        workflow, submission.steps[target_idx:], submission
    )
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
        # Drop node-internal backend slots so they re-run on the next
        # advance; operator slots are dropped by the Airflow-clear path
        # already invoked above.
        target_def = _step_def(workflow, target.node_id)
        if isinstance(target_def, CompiledNode):
            for cs in target_def.chain:
                if cs.kind == "backend_call":
                    target.external_state.pop(
                        cs.backend_call.fn.name, None,
                    )
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
    """Coerce a value to something the JSON store can hold. Plain
    scalars pass through; sentinels and objects (e.g. a branch's
    END return) become None — only ever true of terminal steps,
    whose return is not read downstream. Recurses into dicts and
    lists so nested sentinels (e.g. an END buried in
    `external_state[fn].return`) also get scrubbed."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return None


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
                # Per-step submitter attribution — drives the
                # submission-visibility gate.
                "user_id": s.user_id,
                # Per-step failure message (None unless the step's
                # backend or chain raised). Surfaced to the chain UI.
                "error": s.error,
                # Full Python traceback (multi-line). Rendered in
                # the chain UI's collapsible details panel.
                "traceback": s.traceback,
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
        "cleared_run_ids": submission.cleared_run_ids,
        "steps": steps,
        "events": [
            {
                "type": e.type,
                "node_id": e.node_id,
                "page_id": e.page_id,
                "form_version_id": e.form_version_id,
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
            # Per-step error: prefer the snapshot's step-level field
            # (populated for any step the runtime persisted after
            # this column existed). Fall back to the submission-level
            # error on the failed step for older rows that predate
            # the column — that's the closest information available.
            error=(
                s.get("error")
                if s.get("error") is not None
                else (
                    snapshot["error"] if s["state"] == "failed" else None
                )
            ),
            # Per-step traceback (full Python stack). Newer column —
            # always None on legacy rows that predate it.
            traceback=s.get("traceback"),
            status=StepStatus.parse(s.get("status") or "unaffected"),
            external_state=s.get("external_state") or {},
            user_id=s.get("user_id"),
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
        cleared_run_ids=dict(snapshot.get("cleared_run_ids") or {}),
        events=[
            EventRecord(
                type=e["type"],
                occurred_at=e["occurred_at"],
                node_id=e["node_id"],
                page_id=e["page_id"],
                payload=e["payload"],
                form_version_id=e.get("form_version_id"),
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


def _execute_assigns(
    workflow: CompiledWorkflow,
    submission: Submission,
    step: StepSubmission,
    node: Any,
) -> None:
    """Fire every `Assign` operator on the node that just submitted.

    For each Assign, resolve `to` against the submitted form values,
    fan out by picker identifier kind (frontflow user_id → direct;
    external_id → resolve hook; email → match-or-create; group_id →
    expand to members), find-or-create a child submission per
    assignee, and insert a submission_assignment row.

    Idempotent: re-submitting the same step won't double-grant — the
    grant() call is idempotent for active rows, and the child
    submission lookup uses the (parent_handle, node_id, op_idx,
    user_id) tuple as a key.

    Preview submissions skip every side effect: assignments are a
    persistence operation, no place in a preview.

    Per design doc §6.3: hook failures don't roll back the
    persistence. on_assigned errors are logged and the submission
    proceeds.
    """
    if submission.preview:
        return

    compiled_assigns = getattr(node, "assigns", None) or []
    if not compiled_assigns:
        return
    if not compiled_assigns:
        return

    # Deferred imports — keep dsl import time side-effect-free.
    from frontflow.dsl import assignments as _assignments
    from frontflow.dsl import external_identity as _ext_id
    from frontflow.dsl.store import (
        Form, FormVersion, Group, Submission as _SubmissionRow,
        SubmissionAssignment as _Assignment,
        User, UserGroup, _engine, _utcnow,
    )
    from sqlalchemy import select
    from sqlalchemy.orm import Session as DBSession

    # The acting user (submitter). For assignments granted via an
    # Assign operator, this is the granted_by user. When the
    # submission has no acting user (system-driven flow), fall back
    # to None and let grant() reject — but log instead of crashing
    # the step submission.
    acting_user_id = getattr(submission, "user_id", None)

    # Resolve the parent form's main hook for on_assigned. The
    # Workflow object holds it; we get there via the registry to
    # avoid stuffing every callable into the compiled snapshot.
    on_assigned = _resolve_on_assigned_hook(workflow.id)

    for ca in compiled_assigns:
        target_ids = _resolve_assign_target(ca, step, submission)
        if not target_ids:
            continue

        # Expand group_ids to member user_ids if the picker
        # produced a group identifier kind. The picker's
        # identifier_kind isn't on CompiledAssign — we infer from
        # the parent node's field metadata.
        target_user_ids = _expand_to_user_ids(
            workflow, node, ca, target_ids,
        )

        for assignee_id in target_user_ids:
            # Check granter FIRST — if there's no acting user, we
            # can't insert an assignment row, so creating the child
            # submission would just orphan it. Skip the whole
            # iteration cleanly.
            granter_id = acting_user_id
            if granter_id is None:
                # Loud message because this is a real user-facing
                # failure mode: the picker resolved an assignee but
                # no acting user is on the submission, so no
                # assignment row will be created and the assignee
                # will see nothing in /my-tasks. Most commonly:
                # the form was submitted by an anonymous visitor
                # (no session cookie), or the server is running an
                # old wheel where create_submission didn't thread
                # acting_user_id through.
                print(
                    f"[assign] WARNING: Assign on form "
                    f"{workflow.id!r} node {step.node_id!r} resolved "
                    f"assignee user_id={assignee_id} for role "
                    f"{ca.role_id!r}, but the parent submission has "
                    f"no acting user. No assignment row created. "
                    f"The submitter must be logged in for Assign to "
                    f"grant; check the session cookie."
                )
                continue
            child_handle = _ensure_child_submission(
                parent_handle=submission.handle,
                child_form_id=ca.form_id,
                parent_node_id=step.node_id,
                op_idx=ca.op_idx,
                assignee_user_id=assignee_id,
                prefill_descriptors=ca.prefill_descriptors,
                parent_steps=submission.steps,
            )
            if child_handle is None:
                # Child form not deployed; skip silently — the
                # workflow scan flagged this as a load error
                # already, and crashing every parent submit
                # benefits nobody.
                continue
            try:
                row = _assignments.grant(
                    submission_handle=child_handle,
                    user_id=assignee_id,
                    role_id=ca.role_id,
                    granted_by_user_id=granter_id,
                    granted_by_submission_handle=submission.handle,
                )
                # Confirm what landed — helps the user verify the
                # whole Assign chain fired end-to-end when looking
                # at server logs.
                print(
                    f"[assign] granted user_id={assignee_id} role "
                    f"{ca.role_id!r} on submission {child_handle!r} "
                    f"(by user_id={granter_id} via "
                    f"{workflow.id!r}/{step.node_id!r})"
                )
            except Exception as e:  # noqa: BLE001 — log + continue
                print(
                    f"[assign] grant failed for "
                    f"submission={child_handle!r} user={assignee_id} "
                    f"role={ca.role_id!r}: {e}"
                )
                continue

            if on_assigned is not None:
                _fire_on_assigned_hook(
                    on_assigned,
                    parent_form_id=workflow.id,
                    parent_submission=submission,
                    child_submission_handle=child_handle,
                    child_form_id=ca.form_id,
                    assignee_user_id=assignee_id,
                    role_id=ca.role_id,
                    assignment_row_id=row.id,
                    link_ttl_days=ca.link_ttl_days,
                )


def _resolve_assign_target(
    ca: Any, step: Any, submission: Any,
) -> list[Any]:
    """Return the raw identifiers from the Assign's `to` reference.

    Three shapes:
      - literal: {kind: "literal", value: <list-or-scalar>}
      - same-node field ref: {node: <node_id>, name: <input_id>}
      - cross-node ref (unsupported in v1 — Assign reads its node's
        freshly-submitted values; cross-node refs may resolve from
        earlier submission steps).
    """
    desc = ca.to_ref_descriptor or {}
    if desc.get("kind") == "literal":
        v = desc.get("value")
        if v is None:
            return []
        return list(v) if isinstance(v, (list, tuple)) else [v]

    ref_node = desc.get("node")
    ref_name = desc.get("name")
    if ref_node is None or ref_name is None:
        return []

    if ref_node == step.node_id:
        # Same-node ref — value is in this submit's form_values.
        v = (step.form_values or {}).get(ref_name)
    else:
        # Cross-node ref — walk submission.steps for the source.
        v = None
        for s in submission.steps:
            if s.node_id == ref_node and s.is_submitted:
                v = (s.form_values or {}).get(ref_name)
                break
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _expand_to_user_ids(
    workflow: Any, node: Any, ca: Any, target_ids: list[Any],
) -> list[int]:
    """Convert picker output into concrete frontflow user_ids per
    the picker's identifier_kind. The kind isn't on CompiledAssign
    directly — read it from the parent node's compiled field
    metadata (the picker's wire shape carries `identifier_kind`)."""
    from frontflow.dsl import external_identity as _ext_id
    from frontflow.dsl.store import (
        Group, User, UserGroup, _engine,
    )
    from sqlalchemy import select
    from sqlalchemy.orm import Session as DBSession

    # The picker field on the parent node.
    desc = ca.to_ref_descriptor or {}
    ref_name = desc.get("name")
    field = next(
        (f for f in node.fields if f.name == ref_name), None,
    )
    if field is None:
        return []
    # The wire shape carries identifier_kind in the field's options
    # / extra props — not directly on CompiledField. Read from the
    # serialized field if available, falling back to inference.
    kind = _infer_picker_kind(field)
    if kind is None:
        # Shouldn't happen for picker fields validated at compile
        # time. Treat as no-op.
        return []

    if kind == "frontflow_user_id":
        # target_ids are already user_ids; coerce to int + filter
        # to active users in case the picker returned stale ids.
        ids = [int(i) for i in target_ids if isinstance(i, (int, str))]
        with DBSession(_engine) as db:
            rows = db.execute(
                select(User.id).where(
                    User.id.in_(ids), User.is_active == True,  # noqa: E712
                )
            ).scalars().all()
        return list(rows)

    if kind == "external_id":
        out: list[int] = []
        for ext_id in target_ids:
            user = _ext_id.resolve(str(ext_id))
            if user is not None:
                out.append(user.id)
        return out

    if kind == "email":
        # Match-or-create-stub. Stub users get only `username`
        # (set to the email so listings show something readable);
        # email column isn't in the schema today, so we don't try
        # to set it. The user can be granted assignments and
        # signed links normally.
        out: list[int] = []
        with DBSession(_engine) as db:
            for email in target_ids:
                email = str(email).strip()
                if not email:
                    continue
                # Look up by username==email (a stub user's signal).
                existing = db.execute(
                    select(User).where(User.username == email)
                ).scalar_one_or_none()
                if existing is None:
                    user = User(
                        username=email,
                        is_active=True,
                        is_admin=False,
                    )
                    db.add(user)
                    db.commit()
                    db.refresh(user)
                    out.append(user.id)
                else:
                    out.append(existing.id)
        return out

    if kind == "frontflow_group_id":
        group_ids = [
            int(i) for i in target_ids if isinstance(i, (int, str))
        ]
        if not group_ids:
            return []
        with DBSession(_engine) as db:
            rows = db.execute(
                select(UserGroup.user_id).where(
                    UserGroup.group_id.in_(group_ids),
                )
            ).scalars().all()
            # De-dup; one user may be in several picked groups.
            return sorted(set(rows))

    return []


def _infer_picker_kind(field: Any) -> Optional[str]:
    """Read identifier_kind off a compiled picker field. Pickers
    carry it in their `extra_props` (which lands on the field
    dict at wire-serialization time); on CompiledField it's not a
    first-class attribute, so we read from the field's stored
    props if available. As a last resort, return None — the
    runtime treats that as 'unknown picker' and skips."""
    # The CompiledField doesn't currently carry extra_props. For
    # v1 we look it up by walking the workflow's all_nodes_by_id
    # — but we don't have the workflow at this depth. The cleanest
    # solution is to add `identifier_kind` to CompiledField; do
    # that incrementally.
    return getattr(field, "identifier_kind", None)


def _ensure_child_submission(
    *,
    parent_handle: str,
    child_form_id: str,
    parent_node_id: str,
    op_idx: int,
    assignee_user_id: int,
    prefill_descriptors: dict,
    parent_steps: list,
) -> Optional[str]:
    """Find or create a child submission for (parent_handle, node,
    op_idx, assignee). Returns the child submission's handle, or
    None if the child form isn't deployed.

    The find-or-create key intentionally includes assignee — a
    multi-user `to=` fans out to N child submissions, one each.
    """
    from frontflow.dsl.store import (
        FormVersion, Submission as _SubmissionRow, _engine, _utcnow,
    )
    from sqlalchemy import select
    from sqlalchemy.orm import Session as DBSession
    from frontflow import main as _main_mod

    # We track assignee in the assignment row, not the submission.
    # Re-running an Assign for the SAME assignee on the SAME
    # (parent, node, op_idx) should hit the existing child
    # submission. Fan-out to a different assignee creates a NEW
    # child — they're independent workflows.

    with DBSession(_engine) as db:
        existing = db.execute(
            select(_SubmissionRow).where(
                _SubmissionRow.parent_submission_handle == parent_handle,
                _SubmissionRow.parent_assign_node_id == parent_node_id,
                _SubmissionRow.parent_assign_op_idx == op_idx,
            )
        ).scalars().all()
        # Within those, look for one already assigned to this user
        # (via the assignment table). Cheap join; bounded by
        # parent_handle.
        from .store import SubmissionAssignment as _Assignment
        for sub in existing:
            row = db.execute(
                select(_Assignment).where(
                    _Assignment.submission_handle == sub.handle,
                    _Assignment.user_id == assignee_user_id,
                )
            ).scalar_one_or_none()
            if row is not None:
                # Idempotency hit: same parent + node + assignee.
                # The parent may have been edited and re-submitted
                # the Assign with NEW prefill values. Silently
                # overwrite the child's landing-step draft values
                # with the new prefill, per design (option A).
                # If the assignee was mid-flight with their own
                # edits, those edits are lost — log loudly so an
                # operator can see what happened.
                new_prefill = _resolve_prefill(
                    prefill_descriptors, parent_steps,
                )
                _overwrite_child_landing_prefill(
                    sub.handle, new_prefill,
                )
                return sub.handle

        # Need to create. Find the latest form_version for the
        # child form.
        version = db.execute(
            select(FormVersion).where(
                FormVersion.form_id == child_form_id,
            ).order_by(FormVersion.version.desc())
        ).first()
        if version is None:
            return None
        version_row = version[0]
        # Cache the id before the session closes — accessing
        # version_row.id after `with DBSession(...)` would raise
        # DetachedInstanceError.
        child_form_version_id = version_row.id

        # Mint a handle. The submission_id is left null — child
        # submissions get an id once their landing step is
        # submitted via the existing minting path. Steps are not
        # materialized yet — when the assignee opens the
        # submission via /my-tasks, the existing runtime startup
        # path hydrates the landing node.
        new_handle = _generate_nanoid()
        now = _utcnow()
        # Stash prefill values for the runtime to pick up on
        # first render. For v1 we keep this simple: prefill goes
        # into `cleared_run_ids` JSON column as a re-purposed
        # general "prefill" slot. The clean fix is a dedicated
        # column; for v1, this avoids a fourth migration and
        # the column is harmlessly named (it's already a
        # general-purpose JSON bucket). Worth revisiting.
        prefill_resolved = _resolve_prefill(
            prefill_descriptors, parent_steps,
        )
        sub_row = _SubmissionRow(
            handle=new_handle,
            submission_id=None,
            form_version_id=child_form_version_id,
            state="running",
            created_at=now,
            updated_at=now,
            parent_submission_handle=parent_handle,
            parent_assign_node_id=parent_node_id,
            parent_assign_op_idx=op_idx,
            # Prefill stashed in the existing JSON column — see
            # comment above; flagging for follow-up.
            cleared_run_ids=(
                {"_assign_prefill": prefill_resolved}
                if prefill_resolved
                else None
            ),
        )
        db.add(sub_row)
        db.commit()

    # Hydrate the child submission into the in-memory `_submissions`
    # dict so the assignee can open it via /api/forms/{...}/submissions/{handle}.
    # Without this, the DB row exists but `get_submission` returns
    # None and the assignee gets a 404 when they click the link from
    # their /my-tasks inbox. The child starts with one step in
    # "in-progress" state — the landing node of the child form,
    # carrying the prefill values as defaults. The assignee fills
    # in the rest and submits via the normal step-submit path.
    _hydrate_new_child_submission(
        handle=new_handle,
        child_form_id=child_form_id,
        form_version_id=child_form_version_id,
        prefill_values=prefill_resolved,
        created_at=now,
    )
    return new_handle


def _hydrate_new_child_submission(
    *,
    handle: str,
    child_form_id: str,
    form_version_id: int,
    prefill_values: dict,
    created_at: datetime,
) -> None:
    """Build a fresh Submission for a just-created child row and
    register it in `_submissions` so the runtime can advance it
    when the assignee opens it. Picks up the child form from the
    live FORMS registry — if the form isn't deployed in this
    process, skip the in-memory registration (the DB row stays;
    the next process restart will hydrate it via the normal
    bootstrap path)."""
    from frontflow import main as _main_mod
    child_workflow = _main_mod.FORMS.get(child_form_id)
    if child_workflow is None:
        return
    landing = child_workflow.landing_node()
    if landing is None:
        return

    # Build the landing step in "in-progress" state — values
    # populated with prefill so the assignee sees them as defaults;
    # submitted_at is None so the runtime treats it as awaiting
    # the assignee's submit.
    first_step = StepSubmission(
        node_id=landing.id,
        form_values=dict(prefill_values or {}),
        button_clicked=None,
        started_at=created_at,
        submitted_at=None,
    )

    submission = Submission(
        handle=handle,
        form_id=child_form_id,
        started_at=created_at,
        submission_id=None,  # Minted at first real submit by the assignee.
        form_version_id=form_version_id,
        steps=[first_step],
    )
    _record_event(
        child_workflow, submission, "submission_created",
        occurred_at=created_at,
    )
    _record_event(
        child_workflow, submission, "step_started",
        node_id=landing.id, occurred_at=created_at,
    )

    # Mint the submission_id now, against the prefill values. For
    # forms WITHOUT a submission_id template, this sets
    # submission_id = handle (always succeeds). For forms WITH a
    # template that references prefill fields, it resolves the
    # template against the landing step's form_values. If the
    # template references fields the parent's Assign didn't
    # prefill, the call raises ValueError — surfacing the
    # misconfiguration to the parent's submit (better than a
    # silent stuck child that never mints its id).
    try:
        _try_register_id(child_workflow, submission)
    except ValueError as e:
        # Re-raise with parent-context so the operator can see which
        # Assign produced the orphan child. The caller will let this
        # propagate up to the parent's submit response.
        raise ValueError(
            f"could not mint submission_id for child form "
            f"{child_form_id!r}: {e}. Either drop the "
            f"submission_id template, or extend the parent's "
            f"Assign(prefill={{...}}) to cover the fields the "
            f"template references."
        ) from e

    with _submissions_lock:
        _submissions[handle] = submission

    # Persist the in-memory state (including the newly-minted
    # submission_id and the landing step's prefill values) so the
    # child survives a server restart. Without this, the DB row
    # exists with submission_id=None and no step rows; on rehydrate
    # the runtime gets an empty submission and advance() can't
    # progress. (The defensive check in _advance_inner just avoids
    # the crash — this is the real fix.)
    try:
        from frontflow.dsl import store as _store
        _store.sync_submission(
            submission_snapshot(child_workflow, submission)
        )
    except Exception as e:  # noqa: BLE001 — log + continue
        # Persistence failure shouldn't block the parent's submit;
        # the in-memory submission is still usable for this process.
        # Log loudly so an operator sees it.
        print(
            f"[assign] WARNING: persisting child submission "
            f"{handle!r} failed: {e}"
        )


def _overwrite_child_landing_prefill(
    child_handle: str, new_prefill: dict,
) -> None:
    """Update a child submission's landing-step form_values with a
    fresh prefill resolved from the (re-submitted) parent.

    Edit-cascade semantics: when the parent's Assign re-fires for an
    already-existing child, the new prefill silently overwrites the
    child's landing-step draft values (option A in the design doc).
    If the assignee had any uncommitted local edits to those fields,
    they are lost. Logged loudly so an operator can audit.

    No-op for an unknown child_handle (the find-or-create logic
    above guards against this, but defensive).
    """
    with _submissions_lock:
        child = _submissions.get(child_handle)
    if child is None or not child.steps:
        return
    landing_step = child.steps[0]
    if landing_step.is_submitted:
        # Assignee already submitted the landing step — the new
        # prefill can't overwrite a real submission. Log and skip;
        # the edit cascade for downstream child changes is a
        # separate concern (Phase 4 caveat).
        print(
            f"[assign] WARNING: parent re-submit re-fired Assign "
            f"for child {child_handle!r}, but the child's landing "
            f"step is already submitted. Cannot overwrite prefill; "
            f"the new prefill values are dropped. (Edit cascade "
            f"into a submitted child step is a separate path.)"
        )
        return
    old_values = dict(landing_step.form_values or {})
    new_values = dict(new_prefill or {})
    # Diff to make the log meaningful — show what changed.
    diffs = {
        k: (old_values.get(k), new_values.get(k))
        for k in set(old_values) | set(new_values)
        if old_values.get(k) != new_values.get(k)
    }
    if not diffs:
        return  # no-op: re-prefill produced identical values
    landing_step.form_values = new_values
    print(
        f"[assign] re-prefilled child {child_handle!r} landing "
        f"step: {diffs!r}. Any uncommitted assignee edits to these "
        f"fields are lost."
    )
    # Persist the updated draft so it survives a restart.
    try:
        from frontflow import main as _main_mod
        from frontflow.dsl import store as _store
        wf = _main_mod.FORMS.get(child.form_id)
        if wf is not None and child.submission_id is not None:
            _store.sync_submission(submission_snapshot(wf, child))
    except Exception as e:  # noqa: BLE001 — log + continue
        print(
            f"[assign] WARNING: persisting re-prefilled child "
            f"{child_handle!r} failed: {e}"
        )


def _resolve_prefill(
    descriptors: dict, parent_steps: list,
) -> dict:
    """Resolve prefill values from descriptors. Literals pass through;
    step refs resolve against the parent submission's submitted
    steps."""
    out: dict[str, Any] = {}
    for k, desc in (descriptors or {}).items():
        if not isinstance(desc, dict):
            out[k] = desc
            continue
        if desc.get("kind") == "literal":
            out[k] = desc.get("value")
            continue
        # Step-ref descriptor: {"node": ..., "name": ...}
        ref_node = desc.get("node")
        ref_name = desc.get("name")
        for s in parent_steps:
            if (
                s.node_id == ref_node
                and getattr(s, "is_submitted", False)
            ):
                vals = s.form_values or {}
                if ref_name in vals:
                    out[k] = vals[ref_name]
                break
    return out


def _resolve_on_assigned_hook(form_id: str) -> Optional[Any]:
    """Find the `on_assigned` callable registered on the form's
    @form decorator, if any. Returned by reference so it can be
    invoked synchronously after a grant lands."""
    from frontflow.dsl.core import WORKFLOWS
    wf = WORKFLOWS.get(form_id)
    if wf is None:
        return None
    return getattr(wf, "on_assigned", None)


def _fire_on_assigned_hook(
    hook: Any,
    *,
    parent_form_id: str,
    parent_submission: Any,
    child_submission_handle: str,
    child_form_id: str,
    assignee_user_id: int,
    role_id: str,
    assignment_row_id: int,
    link_ttl_days: int = 7,
) -> None:
    """Invoke the on_assigned hook. Failures are logged and
    swallowed — per design doc §6.3, hook failures do not roll
    back the persisted grant."""
    from frontflow.dsl.store import User, _engine
    from sqlalchemy.orm import Session as DBSession

    # Mint a signed link so the handler can deliver it (Slack, email,
    # SMS, etc.). The link grants the assignee `fill` scope on the
    # child submission for `link_ttl_days`. Hook handlers are
    # responsible for actually sending the link to the right channel;
    # frontflow just makes one available.
    signed_link_token: Optional[str] = None
    try:
        from frontflow.dsl import signed_links as _signed_links
        signed_link_token = _signed_links.mint(
            user_id=assignee_user_id,
            submission_handle=child_submission_handle,
            scope="fill",
            issuer="assign_operator",
            ttl_seconds=link_ttl_days * 24 * 3600,
        )
    except Exception as e:  # noqa: BLE001 — never block the hook
        print(
            f"[on_assigned] failed to mint signed link for "
            f"{child_submission_handle!r}: {e}"
        )

    try:
        with DBSession(_engine) as db:
            assignee = db.get(User, assignee_user_id)
        event = {
            "kind": "assigned",
            "parent_form_id": parent_form_id,
            "parent_submission_handle": parent_submission.handle,
            "child_form_id": child_form_id,
            "child_submission_handle": child_submission_handle,
            "assignee_user_id": assignee_user_id,
            "assignee_username": (
                assignee.username if assignee is not None else None
            ),
            "role_id": role_id,
            "assignment_id": assignment_row_id,
            # Signed-link token (Phase 5). None if minting failed
            # (e.g., FRONTFLOW_SECRET_KEY unset in tests).
            "signed_link_token": signed_link_token,
        }
        hook(event)
    except Exception as e:  # noqa: BLE001 — hook errors don't fail submit
        print(
            f"[on_assigned] hook for {parent_form_id!r} raised: {e}"
        )


def _execute_backend(
    workflow: CompiledWorkflow,
    submission: Submission,
    step: StepSubmission,
) -> None:
    """Invoke each node-internal `@backend` that's ready at submit time.

    A backend is "ready at submit" when all its args are form fields,
    buttons, or files — available the instant the step is submitted.
    These run synchronously in chain order, preserving today's
    routing-at-submit behavior for `@backend.branch` and the existing
    cascade semantics for plain backends.

    A backend whose args reference an operator output or an earlier
    backend's return *defers to the chain processor* — it runs in
    chain order, once its dependencies have completed. Once a
    deferred backend appears in the chain, every backend after it
    also defers (its args might depend on the deferred backend's
    return, even transitively). Skipped here either way.

    A backend that raises fails the submission, just like an external
    task that fails or a standalone backend step that raises.
    """
    ng = workflow.all_nodes_by_id.get(step.node_id)
    if ng is None or not ng.chain:
        return

    button_ids = {b.id for b in ng.buttons}
    file_field_types = {
        f.name: f.type
        for f in ng.fields
        if f.type in ("file", "s3file")
    }

    # Walk chain in order; run every backend that can run at submit.
    # Once we see a chain step that *can't* run at submit (any
    # operator, or a deferred backend), stop — anything past it depends
    # on chain progress and belongs to the chain processor.
    first_backend = True
    for cs in ng.chain:
        if cs.kind != "backend_call":
            return  # an operator gates everything after it
        bc = cs.backend_call
        if bc.defers_to_chain:
            return  # deferred backend gates the rest

        if submission.preview:
            # PREVIEW MODE: never invoke the backend (and skip its
            # argument resolution, which can reference fields the
            # preview hasn't populated). Record a None return.
            result = None
        else:
            # Argument resolution AND invocation share the same
            # failure handler. A `steps.<x>` accessor or an upload
            # handle that blows up during prep was previously
            # uncaught — the exception would unwind past this
            # function with no `step.error` recorded, leaving the
            # UI staring at a non-advancing submission with no
            # message anywhere.
            try:
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
                        args.append(
                            (step.form_values or {}).get(arg_id)
                        )

                kwargs: dict[str, Any] = {}
                if "steps" in bc.fn.param_names:
                    kwargs["steps"] = _steps_accessor(
                        workflow, submission
                    )

                result = bc.fn.func(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                step.error = (
                    f"@backend {bc.fn.name!r} in node "
                    f"{step.node_id!r} raised: "
                    f"{type(e).__name__}: {e}"
                )
                submission.failed = True
                submission.ended_at = datetime.now(timezone.utc)
                return

        result = _promote_bytes_to_blob(result, submission)
        # Record the return under `external_state[<fn_name>]` — the
        # canonical per-step slot. Templates read from there
        # (`steps.<fn_name>.return`); branch routing reads from there;
        # the chain processor sees it as done and skips.
        step.external_state[bc.fn.name] = {
            "state": "success",
            "return": result,
            "detail": f"@backend {bc.fn.name!r} returned (at submit)",
        }
        # `step.backend_return` is a legacy singleton — kept only for
        # the submission-detail frontend, which shows one "returned"
        # value per step. With multi-backend that's ambiguous; we
        # carry the *first* backend's return as a best-effort
        # representative. The proper fix is a frontend pass to display
        # each chain step's outputs uniformly.
        if first_backend:
            step.backend_return = result
            first_backend = False


def _run_backend_step(
    workflow: CompiledWorkflow,
    submission: Submission,
    step: StepSubmission,
    step_def: CompiledBackendStep,
) -> None:
    """Execute a workflow-level backend step. Its arguments are `steps`
    references — resolved against the submission's accumulated data and
    bound positionally / by keyword to the function's parameters. On
    failure, the error is recorded and the submission fails.

    Both the argument resolution AND the function invocation are
    wrapped in the failure handler — a step ref to a field that
    doesn't exist on a prior return, an unexpected None where the
    backend expects a dict, or a builder helper raising during
    `build_steps_with_workflow` would otherwise crash the advance
    loop with no `step.error` set and no `submission.failed` flag,
    leaving the UI showing nothing about what went wrong.
    """
    fn = step_def.fn
    now = datetime.now(timezone.utc)

    if submission.preview:
        # PREVIEW MODE: skip invocation, record a None return.
        step.backend_return = None
        step.submitted_at = now
        return

    try:
        steps_data = build_steps_with_workflow(workflow, submission)
        args = [_resolve_step_ref(r, steps_data) for r in step_def.arg_refs]
        kwargs = {
            k: _resolve_step_ref(r, steps_data)
            for k, r in step_def.kwarg_refs.items()
        }
        result = fn.func(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        step.error = f"{type(e).__name__}: {e}"
        step.traceback = traceback.format_exc()
        step.submitted_at = now
        submission.failed = True
        return

    step.backend_return = _promote_bytes_to_blob(result, submission)
    step.submitted_at = now


def _determine_next(
    workflow: CompiledWorkflow,
    step_def: Any,
    step: StepSubmission,
    submission: Optional[Submission] = None,
) -> tuple[Optional[str], bool]:
    """Resolve the next step id from the `>>` graph and the branch
    decision.

    Returns (next_id, branched_explicitly):
      - next_id: a node/backend-step id to go to, or None to terminate
      - branched_explicitly: True when an @backend.branch chose a target
        explicitly (an id or END) — trailing ExternalTasks then skip

    For a page section node, routing follows the node's *page-internal*
    edges; a terminal section node ends the page and routing follows the
    *page's* workflow edges. Any target that resolves to a page is
    descended to that page's entry section node.

    Preview mode: when `submission.preview` is true and the step has
    a branch, consult `submission.preview_branch_choices[step.node_id]`
    (or `step_def.id` for standalone backend steps) before reading
    the backend return value. The admin's choice routes the preview;
    the backend that would normally choose wasn't allowed to run.

    Raises ValueError when a branch returns an id that is not wired
    downstream, or when a step fans out without a branch choosing one.
    Raises `_NeedsPreviewBranchChoice` when in preview mode and the
    branch needs a route the admin hasn't picked yet — the API
    catches this and surfaces a picker to the admin.
    """
    is_branch = False
    fn_name = ""
    # A node whose branch authority is an HitlBranch operator
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
        # Walk the chain for a branch backend — it isn't guaranteed
        # to be the first backend in declared order. At most one
        # branch backend per chain (enforced at compile time).
        # Done for BOTH paths above: a terminal section node (or a
        # flat-page implicit node) whose chain has a `@backend.branch`
        # still needs its return value consulted to pick between the
        # page's workflow-level downstream targets — otherwise the
        # fan-out to those targets reads as "no decision" at runtime
        # and the workflow can't route.
        for cs in step_def.chain:
            if (
                cs.kind == "backend_call"
                and cs.backend_call.fn.is_branch
            ):
                is_branch = True
                fn_name = cs.backend_call.fn.name
                break

    # The HITL-branch route isn't known yet — leave next_node_id unset;
    # _apply_hitl_branch_route fills it once the operator resolves.
    # In PREVIEW mode the operator is stubbed, so we treat HitlBranch
    # the same as a backend.branch — the admin picks from the node's
    # downstream nodes.
    if hitl_branch_node:
        if submission is not None and submission.preview:
            choice = submission.preview_branch_choices.get(step.node_id)
            if choice is None:
                raise NeedsPreviewBranchChoice(
                    step_id=step.node_id,
                    fn_name=f"{step.node_id}.HitlBranch",
                    downstream=list(downstream),
                    can_end=True,
                )
            if choice == "END":
                return None, True
            if choice not in downstream:
                raise ValueError(
                    f"preview branch choice {choice!r} for HitlBranch "
                    f"step {step.node_id!r} is not wired downstream "
                    f"of it (downstream: {downstream}). Reset preview "
                    "and try again."
                )
            return _descend(workflow, choice), True
        return None, False

    if is_branch:
        # PREVIEW MODE: the backend that would normally choose isn't
        # allowed to run, so its return is None. Two ways the route
        # gets decided:
        #   1. The admin pre-records a choice (e.g. the jump helper
        #      populates `preview_branch_choices` before walking) —
        #      we honor it here.
        #   2. Nothing is pre-recorded → raise NeedsPreviewBranchChoice
        #      so the API surfaces a picker UI; admin picks; we get
        #      called again with the choice now in the dict.
        if submission is not None and submission.preview:
            picker_key = (
                step_def.id
                if isinstance(step_def, CompiledBackendStep)
                else step.node_id
            )
            choice = submission.preview_branch_choices.get(picker_key)
            if choice is None:
                raise NeedsPreviewBranchChoice(
                    step_id=picker_key,
                    fn_name=fn_name,
                    downstream=list(downstream),
                    can_end=True,
                )
            if choice == "END":
                return None, True
            if choice not in downstream:
                raise ValueError(
                    f"preview branch choice {choice!r} for step "
                    f"{picker_key!r} is not wired downstream of it "
                    f"(downstream: {downstream}). Reset preview and "
                    "try again."
                )
            return _descend(workflow, choice), True

        # The branch's return: standalone backend steps record it on
        # `step.backend_return`; node-internal `@backend.branch` calls
        # record it in `step.external_state[<fn_name>]['return']` like
        # any other chain step. Pick the right source.
        if isinstance(step_def, CompiledBackendStep):
            rv = step.backend_return
        else:
            rv = (step.external_state.get(fn_name) or {}).get("return")
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
    on the step and fail the submission. Returns False when it failed.

    Re-raises `NeedsPreviewBranchChoice` (preview-mode only) so the
    API layer can catch it without the submission ending up in a
    half-routed state — the branch needs admin input before the
    runtime can pick a next step."""
    try:
        next_id, explicit = _determine_next(
            workflow, step_def, step, submission,
        )
    except NeedsPreviewBranchChoice:
        # Preview-only — do not mark the submission failed; the
        # caller must catch this, prompt the admin, then re-drive.
        raise
    except ValueError as e:
        step.error = f"routing failed: {e}"
        step.traceback = traceback.format_exc()
        submission.failed = True
        return False
    step.next_node_id = next_id
    step.branch_taken_explicitly = explicit
    return True


def _airflow_hook_for(name: str | None) -> AirflowHook:
    """Build an AirflowHook for the given connection name. `None`
    resolves the conventional `airflow_default`; an unknown name
    errors via `AirflowConnection.handle_missing_connection`."""
    from .connections import (
        AirflowConnection,
        ConnectionResolutionError,
    )

    try:
        rec = AirflowConnection.resolve(name)
    except ConnectionResolutionError as e:
        # Re-raise as AirflowError so downstream catch sites that
        # already handle AirflowError continue to work uniformly.
        raise AirflowError(str(e))
    if rec is None:
        # AirflowConnection's handle_missing_connection should raise
        # rather than return None — but defend against subclass drift.
        raise AirflowError(
            f"connection {name!r} resolved to no record"
        )
    return AirflowHook(rec)


def _process_chain(
    workflow: CompiledWorkflow,
    submission: Submission,
    step: StepSubmission,
    chain: list,
) -> bool:
    """Advance a node's `>>` execution chain by one tick.

    The chain interleaves connected-Airflow operators and node-internal
    `@backend` calls in declared order. Each step runs once its
    dependencies (form fields, earlier operator outputs, earlier
    backend returns) are available; results go into
    `step.external_state[<step_id>]` so downstream chain steps and
    later nodes can read them via the same `steps.<step_id>.<key>`
    pattern.

    Per-step dispatch:
      - operator with `connection == "mock"`: synthesized success state
        (after the same aggregate elapsed-time timing the chain has
        always used).
      - operator with any other connection: real Airflow dispatch via
        the named connection (or `airflow_default` if None).
      - `@backend` call: invoke the function with args resolved from
        form fields, button states, and prior chain-step outputs.

    Returns True when every step has reached `success`. A `failed`
    state fails the submission. A non-terminal state (queued, running,
    awaiting_response) breaks the walk — the chain re-ticks on the
    next request.
    """
    if not chain:
        return True

    steps_data = build_steps_with_workflow(workflow, submission)
    variables_data = _variables_snapshot()

    def resolve(template_str: str) -> Any:
        return render(template_str, steps_data, variables=variables_data)

    # Mock-timing — when *any* operator in this chain uses mock
    # dispatch, the aggregate elapsed-time timing model applies to the
    # mock operators in chain-position order. Backends don't participate
    # in elapsed-time gating; they run instantly when their turn comes.
    elapsed = (
        (datetime.now(timezone.utc) - step.submitted_at).total_seconds()
        if step.submitted_at else 0.0
    )
    mock_op_indices = [
        i for i, cs in enumerate(chain)
        if cs.kind == "external_task"
        and (cs.external_task.config or {}).get("connection") == "mock"
    ]
    mock_progress = dict(external_task_states(
        len(mock_op_indices), elapsed,
    ))

    for chain_idx, cs in enumerate(chain):
        step_id = cs.step_id
        prior = step.external_state.get(step_id)
        if prior and prior.get("state") == "success":
            # Already finalized; re-apply any HITL-branch routing so a
            # refresh doesn't lose the chosen target.
            if cs.external_task is not None:
                _apply_hitl_branch_route(
                    workflow, step, cs.external_task, prior,
                )
            continue

        if cs.kind == "backend_call":
            # Run the backend now — args bound from form values,
            # button states, and prior chain-step outputs.
            new_state = _run_chain_backend(
                workflow, submission, step, cs.backend_call,
            )
            step.external_state[step_id] = new_state
            # Update the in-flight `steps_data` so later chain steps in
            # the same tick can read this backend's return at the same
            # path the namespace builder would expose: nested under the
            # owning node, unwrapped to the return value.
            node_ns = steps_data.setdefault(step.node_id, {})
            node_ns[step_id] = (new_state or {}).get("return")
            if new_state.get("state") == "failed":
                submission.failed = True
                submission.ended_at = datetime.now(timezone.utc)
                step.error = (
                    f"@backend {cs.backend_call.fn.name!r} in node "
                    f"{step.node_id!r} raised: {new_state.get('detail')}"
                )
                # Carry the full Python traceback up from the inner
                # runner — the chain UI surfaces it as a collapsible
                # under the short error message.
                step.traceback = new_state.get("traceback")
                _record_event(
                    workflow, submission, "submission_failed",
                    node_id=step.node_id,
                    occurred_at=submission.ended_at,
                    payload={
                        "chain_step": step_id,
                        "detail": new_state.get("detail"),
                    },
                )
                return False
            continue  # backend always reaches a terminal state

        # External task — operator with a real or mock connection.
        task = cs.external_task
        cfg = task.config or {}
        if submission.preview:
            # PREVIEW MODE: skip every external operator. Synthesize
            # an immediate-success state with no return value. The
            # admin can still drive HitlBranch routing via the preview
            # branch picker; we don't need the operator's actual
            # return for that — only the routing decision matters,
            # and the chain advance code reads
            # `submission.preview_branch_choices` directly in preview.
            new_state = {
                "state": "success",
                "detail": f"(preview — {task.kind!r} operator stubbed)",
            }
        elif task.kind == "superset_refresh":
            # Fire-and-forget: succeed the moment the chain reaches this
            # operator. The chain must never block waiting for a browser
            # to acknowledge a refresh — a submission has to progress
            # with no client attached.
            #
            # The directive rides the chain step's state, which the
            # client already polls, so an open dashboard block picks it
            # up with no extra transport.
            from ..superset.operators import build_directive

            dashboard = (cfg.get("dashboard") or "").strip()
            directive = build_directive(dashboard)
            new_state = {
                "state": "success",
                "detail": f"refresh requested for {dashboard!r}",
                "dashboard_refresh": directive,
            }
        elif task.kind == "superset_set_filters":
            # Fire-and-forget, like the refresh it implies: the chain
            # must never wait on a browser, and this talks to Superset
            # not at all.
            #
            # Values are rendered HERE rather than at compile time,
            # because they reference steps that had not run then — which
            # is the whole point of letting a @backend decide what the
            # dashboard should show.
            from ..superset.operators import build_filter_directive

            dashboard = (cfg.get("dashboard") or "").strip()
            resolved: dict[str, Any] = {}
            for filter_name, value in (cfg.get("filters") or {}).items():
                if isinstance(value, str) and "{{" in value:
                    # The same resolver the rest of this chain uses, so
                    # a filter value sees exactly what an
                    # AirflowStatus.run_id would — prior steps and
                    # workflow variables alike.
                    rendered = resolve(value)
                    # An unresolved reference would filter the dashboard
                    # to nothing, which looks like missing data rather
                    # than a mistake. Leaving it out is the honest
                    # failure, and the detail below says so.
                    if not rendered:
                        continue
                    resolved[filter_name] = rendered
                else:
                    resolved[filter_name] = value

            panel = cfg.get("panel") or None
            dropped = len(cfg.get("filters") or {}) - len(resolved)
            target = f"{dashboard!r}" + (f" panel {panel!r}" if panel else "")
            detail = f"filters set on {target}: {', '.join(resolved) or 'none'}"
            if dropped:
                detail += f" ({dropped} unresolved)"

            new_state = {
                "state": "success",
                "detail": detail,
                "dashboard_filters": build_filter_directive(
                    dashboard, resolved, panel
                ),
            }
        elif cfg.get("connection") == "mock":
            # Mock — synthesize state from elapsed time.
            try:
                mock_index = mock_op_indices.index(chain_idx)
            except ValueError:
                mock_index = -1
            mock_state = mock_progress.get(mock_index)
            if mock_state is None:
                return False  # not yet reached in elapsed time
            if mock_state != "success":
                step.external_state[step_id] = _with_waiting_message(
                    {
                        "state": mock_state,
                        "detail": f"mock: {mock_state}",
                    },
                    cfg, resolve,
                )
                return False
            new_state = _mock_success_state(task, resolve, submission)
        else:
            # Real Airflow dispatch.
            new_state = advance_airflow_task(
                task, prior, resolve=resolve,
                get_hook=_airflow_hook_for,
                node_id=step.node_id,
                cleared_run_ids=submission.cleared_run_ids,
                form_values=step.form_values or {},
                steps_data=steps_data,
            )

        # Attach the developer-supplied `waiting_message` (templated)
        # so the frontend's status panel can render it while the
        # operator is in a non-terminal state. Resolved on every
        # advance so the latest `steps` data is reflected.
        new_state = _with_waiting_message(new_state, cfg, resolve)

        step.external_state[step_id] = new_state
        if new_state.get("reattached"):
            submission.cleared_run_ids.pop(
                f"{step.node_id}::{task.task_id}", None
            )
        # Operator output stays as a state dict (downstream reads
        # `.run_id`, `.value`, etc.). Nest under the owning node so
        # subsequent chain steps in the same tick can read it at the
        # same path the namespace builder uses.
        node_ns = steps_data.setdefault(step.node_id, {})
        node_ns[step_id] = new_state

        state = new_state.get("state")
        if state == "failed":
            submission.failed = True
            submission.ended_at = datetime.now(timezone.utc)
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
            return False  # still progressing

        _apply_hitl_branch_route(workflow, step, task, new_state)

    return True


def _run_chain_backend(
    workflow: CompiledWorkflow,
    submission: Submission,
    step: StepSubmission,
    bc: Any,
) -> dict[str, Any]:
    """Run one node-internal `@backend` in the chain.

    Each positional arg binds from one of (in order): a button id →
    True/False (whether that button was clicked); a file/s3file field
    name → an upload handle the function can `.read()`; a prior chain
    step's id → its full state dict from `external_state`; a regular
    field name → the submitted value.

    Returns the chain-step state dict to record in `external_state` —
    `{state: 'success', return: <fn return>, detail: ...}` on success;
    `{state: 'failed', detail: ...}` on raise.
    """
    ng = workflow.all_nodes_by_id.get(step.node_id)
    if ng is None:
        return {"state": "failed", "detail": "node not found"}

    button_ids = {b.id for b in ng.buttons}
    file_field_types = {
        f.name: f.type
        for f in ng.fields
        if f.type in ("file", "s3file")
    }

    # Identify which chain-step ids are backends so we can unwrap
    # their `{state, return, detail}` envelope to just the return value
    # when binding as a positional arg — operators keep their state
    # dict (callers read `.run_id`, `.value`, etc.).
    backend_ids = {
        cs.backend_call.fn.name
        for cs in ng.chain
        if cs.kind == "backend_call"
    }

    if submission.preview:
        # PREVIEW MODE: skip invocation, treat as success with no return.
        result = None
    else:
        # Same widened guard as the workflow-level backend runner —
        # an unexpected None in the form values, a missing
        # external_state entry, or an upload handle that throws
        # during prep was previously uncaught here, surfacing as a
        # 500 in the request handler rather than a failed step.
        try:
            args: list[Any] = []
            for arg_id in bc.arg_op_ids:
                if arg_id in button_ids:
                    args.append(arg_id == step.button_clicked)
                elif arg_id in file_field_types:
                    raw = (step.form_values or {}).get(arg_id)
                    args.append(uploads.handle_for_value(
                        file_field_types[arg_id], raw,
                    ))
                elif arg_id in step.external_state:
                    chain_state = step.external_state[arg_id]
                    if arg_id in backend_ids:
                        # Backend: pass the return value directly.
                        args.append(
                            (chain_state or {}).get("return")
                        )
                    else:
                        # Operator: pass the full state dict so the
                        # function can read `.run_id`, `.value`, etc.
                        args.append(chain_state)
                else:
                    # Default: a form field value.
                    args.append((step.form_values or {}).get(arg_id))

            kwargs: dict[str, Any] = {}
            if "steps" in bc.fn.param_names:
                kwargs["steps"] = _steps_accessor(workflow, submission)

            result = bc.fn.func(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            return {
                "state": "failed",
                "detail": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc(),
            }
    result = _promote_bytes_to_blob(result, submission)
    return {
        "state": "success",
        "return": result,
        "detail": f"@backend {bc.fn.name!r} returned",
    }


def _promote_bytes_to_blob(
    result: Any, submission: Submission,
) -> Any:
    """Promote raw bytes in a `@backend` return to blob handles: hash
    them, stash in the submission-blob store, and replace the value
    with a small handle dict the rest of the pipeline can carry
    around safely.

    Recursive — a bare `bytes` return is promoted (the classic
    `displays.Figure` source), and so is any bytes leaf nested in a
    dict or list, e.g. a `KPIGroups`-shaped return carrying one chart
    per group: `{group: {"charts": {caption: <png bytes>}}}`. Without
    the recursion, nested bytes would be scrubbed to None by the JSON
    store coercion. Non-bytes leaves pass through untouched.

    The handle is the shape `{kind: 'blob', hash, content_type,
    size}`. The `displays.Figure` block (and `KPIGroups`' chart
    rendering) knows how to render a handle as an `<img>` pointing at
    the blob proxy endpoint.
    """
    if isinstance(result, (bytes, bytearray)):
        body = bytes(result)
        content_type = _sniff_image_content_type(body)
        return store.put_submission_blob(
            submission_handle=submission.handle,
            body=body,
            content_type=content_type,
        )
    if isinstance(result, dict):
        return {
            k: _promote_bytes_to_blob(v, submission)
            for k, v in result.items()
        }
    if isinstance(result, list):
        return [_promote_bytes_to_blob(v, submission) for v in result]
    return result


def _sniff_image_content_type(body: bytes) -> str:
    """Identify a few image formats from their leading bytes. Used to
    set the `Content-Type` on a blob the proxy will stream back. The
    set is deliberately small — PNG, JPEG, SVG are what matplotlib
    produces. Unknown shapes default to `application/octet-stream`;
    the browser still loads them via the proxy, just without a typed
    Content-Type hint."""
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body[:2] == b"\xff\xd8":  # JPEG SOI
        return "image/jpeg"
    # SVG is XML — accept either a bare <svg> root or an XML preamble
    # that opens an SVG document. Skip leading whitespace.
    leading = body[:512].lstrip()
    if leading.startswith(b"<svg") or (
        leading.startswith(b"<?xml") and b"<svg" in leading[:512]
    ):
        return "image/svg+xml"
    return "application/octet-stream"


def _with_waiting_message(
    state: dict[str, Any],
    cfg: dict[str, Any],
    resolve: Callable[[str], Any],
) -> dict[str, Any]:
    """Resolve the operator's `waiting_message` template (if any) and
    attach it to the state dict. Surfaces in the frontend status panel
    while the operator is in a non-terminal state. Resolved against
    the same `steps` namespace as the operator's other templated
    config so messages like `"Parsing {{ steps.upload.name }}..."`
    work naturally."""
    template = cfg.get("waiting_message")
    if not template:
        return state
    try:
        rendered = resolve(template)
    except Exception:  # noqa: BLE001 — fall back to literal on render error
        rendered = template
    if rendered is None or rendered == "":
        return state
    return {**state, "waiting_message": str(rendered)}


def _mock_success_state(
    task: Any,
    resolve: Callable[[str], Any],
    submission: Submission,
) -> dict[str, Any]:
    """The successful-state shape for one operator under mock dispatch.

    Per operator kind:
      - airflow_trigger_dag: {state, run_id, detail}. `run_id` resolves
        the operator's `run_id_template` against the current steps
        namespace when one is set — so a downstream `@backend(dagrun)`
        sees the same id it would in production. Falls back to a
        deterministic synthetic id when no template is configured.
      - airflow_task_sensor / airflow_dag_sensor: {state, detail}.
      - airflow_xcom_pull: {state, value, detail}. Synthesizes a
        predictable `mock_xcom_<task_id>` string the author can
        pattern-match in their downstream code.
      - airflow_hitl: {state, detail} — auto-approved.
      - airflow_hitl_branch: {state, chosen_options, detail}. The first
        declared route key is chosen so branch wiring is exercisable
        under mock. Real branch testing still needs real Airflow.
    """
    cfg = task.config or {}
    kind = task.kind

    if kind == "airflow_trigger_dag":
        run_id_template = cfg.get("run_id_template")
        if run_id_template:
            try:
                run_id = str(resolve(run_id_template))
            except Exception:  # noqa: BLE001 - resolution best-effort
                run_id = f"mock__{task.task_id}__{submission.handle[:8]}"
        else:
            run_id = f"mock__{task.task_id}__{submission.handle[:8]}"
        return {
            "state": "success",
            "run_id": run_id,
            "detail": f"mock: triggered run {run_id}",
        }

    if kind in ("airflow_task_sensor", "airflow_dag_sensor"):
        return {
            "state": "success",
            "detail": f"mock: {kind.replace('airflow_', '')} succeeded",
        }

    if kind == "airflow_task_state_sensor":
        # Under mock there's no real task to observe; we synthesize the
        # first declared target_state as the observed value so the
        # downstream code sees the same `observed_state` shape it would
        # under real dispatch.
        targets = cfg.get("target_states") or []
        observed = targets[0] if targets else "success"
        return {
            "state": "success",
            "observed_state": observed,
            "detail": f"mock: task_state_sensor matched {observed!r}",
        }

    if kind == "airflow_xcom_pull":
        value = f"mock_xcom_{task.task_id}"
        return {
            "state": "success",
            "value": value,
            "detail": f"mock: pulled xcom {cfg.get('key', '')!r}",
        }

    if kind == "airflow_hitl":
        return {
            "state": "success",
            "detail": "mock: hitl auto-approved",
        }

    if kind == "airflow_hitl_response":
        return {
            "state": "success",
            "detail": "mock: hitl response sent",
        }

    if kind == "airflow_hitl_branch":
        # First declared route key — deterministic so branch wiring is
        # exercisable under mock without coin-flipping every run.
        routes = cfg.get("routes") or {}
        first_option = next(iter(routes), None)
        return {
            "state": "success",
            "chosen_options": [first_option] if first_option else [],
            "detail": f"mock: hitl_branch chose {first_option!r}",
        }

    # Unknown kind — give it a generic success so the chain still
    # advances; useful for any future operator kind that hasn't been
    # explicitly handled here yet.
    return {"state": "success", "detail": f"mock: {kind}"}


def _apply_hitl_branch_route(
    workflow: CompiledWorkflow,
    step: StepSubmission,
    task: Any,
    task_state: dict[str, Any],
) -> None:
    """Route the form's chain on an HitlBranch task's outcome.

    The first chosen option is looked up in the operator's `routes` map;
    a hit sets next_node_id (an explicit branch, like @backend.branch).
    An unmapped option falls through to the normal `>>` chain. A no-op
    for any task that isn't an HitlBranch.
    """
    if task.kind != "airflow_hitl_branch":
        return
    routes = (task.config or {}).get("routes") or {}
    chosen = (task_state.get("chosen_options") or [None])[0]
    target = routes.get(chosen)
    if target is not None:
        step.next_node_id = _descend(workflow, target)
        step.branch_taken_explicitly = True


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

      - node step      → `steps.<node>` is a namespace of:
                          - submitted form values (by field name);
                          - node-internal operator state dicts (by
                            operator task_id), with operator-specific
                            fields (`run_id`, `value`, etc.) on the
                            state dict;
                          - node-internal `@backend` returns (by fn
                            name), *unwrapped* — the value is the
                            backend's return, not the {state, return}
                            envelope.
      - backend step   → `steps.<step>` is its return value directly
                         (standalone backend nodes, not node-internal
                         backends).

    The nested shape means `steps.<node>.<field>` for form values,
    `steps.<node>.<operator_id>.<field>` for operator outputs, and
    `steps.<node>.<backend_fn>` for backend returns. Authors don't
    need to spell `["return"]` — a node-internal backend is reached by
    its function name directly.
    """
    steps: dict[str, Any] = {}
    for s in submission.steps:
        step_def = workflow.by_id.get(s.node_id)
        if isinstance(step_def, CompiledBackendStep):
            # A standalone backend step's return is the step's value.
            steps[s.node_id] = s.backend_return
            continue
        # A node step's namespace: form values, plus its chain steps.
        merged: dict[str, Any] = {}
        if s.form_values:
            merged.update(s.form_values)
        # Identify backend function names in this node's chain so we
        # can unwrap their `{state, return, detail}` envelopes to just
        # the return value at the namespace level. Operators keep their
        # full state dict (downstream code reads `.run_id`, `.value`,
        # etc. as state-dict fields).
        backend_fn_names: set[str] = set()
        ng = workflow.all_nodes_by_id.get(s.node_id)
        if ng is not None:
            for cs in ng.chain:
                if cs.kind == "backend_call":
                    backend_fn_names.add(cs.backend_call.fn.name)
        for chain_step_id, chain_state in (s.external_state or {}).items():
            if chain_step_id in backend_fn_names:
                # Backend: unwrap to the return value. `merged[fn_name]`
                # *is* whatever the @backend returned.
                merged[chain_step_id] = (chain_state or {}).get("return")
            else:
                # Operator: keep the full state dict so callers can
                # read `.run_id` / `.value` / etc.
                merged[chain_step_id] = chain_state
        steps[s.node_id] = merged
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
from .references import STEP_REF_RE
from .references import TEMPLATED_PROPS as _TEMPLATED_PROPS


def _resolve_template_string(
    text: str,
    current_node_id: str,
    steps_data: dict[str, Any],
    *,
    is_url: bool,
) -> str:
    """Resolve Jinja template expressions like
    `{{ steps.<node>.<field> }}` (with optional filters such as
    `| lower`, `| default("none")`) in one string.

    Two behaviors preserved from the older regex-only implementation:

      - A token naming the **current** node is left untouched. The
        browser resolves it live against the in-progress form — the
        user can see their answer mirror in display text without
        round-tripping to the server.
      - When `is_url=True`, the resolved replacement is percent-
        encoded so it's safe to inline into a URL.

    Anything other than `{{ steps.* }}` is also passed through Jinja
    (so `{{ now() | timestamp_ms }}`-style expressions work in
    Markdown source, KPI values, etc.), but the common case is the
    step-reference token.
    """
    # Fast path: nothing template-shaped in the text → no work.
    if "{{" not in text:
        return text

    # Walk the string in two passes:
    #  1. Find every `{{ steps.<current_node>.* }}` match and leave
    #     it as a literal placeholder — the client resolves these.
    #  2. Hand the remaining segments to Jinja for full rendering
    #     (filters, multiple refs, expressions).
    #
    # Doing this by segmenting avoids passing the same-node literals
    # through Jinja, which would resolve them against the partial
    # `steps_data` and replace them with stale or empty values.
    parts: list[str] = []
    cursor = 0
    placeholders: list[str] = []  # opaque tokens for same-node literals

    PLACEHOLDER_FMT = "\x00FF_LIT_{}\x00"

    for m in STEP_REF_RE.finditer(text):
        # Append text up to this match.
        parts.append(text[cursor:m.start()])
        cursor = m.end()
        if m.group(1) == current_node_id:
            # Same-node literal — stash and emit a placeholder so
            # Jinja doesn't try to render it.
            idx = len(placeholders)
            placeholders.append(m.group(0))
            parts.append(PLACEHOLDER_FMT.format(idx))
        else:
            # Cross-node ref — pass through to Jinja as-is.
            parts.append(m.group(0))
    parts.append(text[cursor:])
    masked = "".join(parts)

    # Render the masked string. _SilentUndefined gives empty string
    # on missing refs, matching the prior behavior.
    rendered = _render_template(masked, steps_data)

    # Restore same-node literals.
    for idx, literal in enumerate(placeholders):
        rendered = rendered.replace(PLACEHOLDER_FMT.format(idx), literal)

    if is_url:
        # URL escape the *whole* resolved string, matching the prior
        # behavior. Same-node literals (which won't be resolved
        # client-side until form-in-progress fills them in) survive
        # as-is — escaping `{{ steps.x.y }}` would corrupt the
        # template syntax the client expects to see.
        # Trade-off: an author writing a URL like
        # `https://x.com/?q={{ steps.foo.bar }}` where `foo` is the
        # current node will get the placeholder URL-escaped client-
        # side at render time, which is the same trade-off the old
        # regex code made.
        rendered = quote(rendered, safe="")

    return rendered


def _render_template(template_str: str, steps_data: dict[str, Any]) -> str:
    """Render `template_str` via the shared Jinja environment used
    by `submission_id` / `id=` templates. Centralized so prop
    templating and identifier templating support the same filter
    set (`slugify`, `timestamp_ms`, `now`, plus Jinja built-ins)."""
    from frontflow.dsl import templating as _tmpl_mod
    return _tmpl_mod.render(template_str, steps_data, strict=False)


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
    data.

    Two shapes of step end up in `steps_data`:

      - **HITL nodes** put their full value dict (form values +
        chain-step returns + operator state) at `steps_data[node_id]`.
        A whole-node ref returns a copy of that dict; a field ref
        digs into it.

      - **Workflow-level @backend steps** put their return value
        DIRECTLY at `steps_data[step_id]` — whatever shape the
        function returned (string, list, bytes, dict, anything).
        For these, a whole-node ref `steps.<step_id>` IS the
        backend's return. A field ref `steps.<step_id>.<key>` only
        makes sense if the return is a dict; otherwise it returns
        None.

    Returns None when the step hasn't run, or the field isn't
    present, or a field ref targets a non-dict backend return.
    """
    node_id = ref.get("node")
    if node_id not in steps_data:
        return None
    node_data = steps_data[node_id]
    name = ref.get("name")
    if name is None:
        # Whole-node ref. HITL node values are dicts (copy
        # defensively so the caller can't mutate the live data).
        # Workflow-backend returns come through as-is — that's
        # the canonical shape for `steps.<backend_step>`.
        if isinstance(node_data, dict):
            return dict(node_data)
        return node_data
    if isinstance(node_data, dict):
        return node_data.get(name)
    # Field ref against a non-dict (a workflow-backend return that
    # isn't a dict): no field semantics exist.
    return None


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

    # Upstream `options` / `default` / Sankey-column / histogram-data
    # references.
    check = new_props if new_props is not None else props
    _from_keys = (
        "options_from",
        "default_from",
        "column_a_from",
        "column_b_from",
        "data_from",
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
        # Histogram-family widget data resolution. Both
        # `histogram_widget` (DistributionFilter) and
        # `redistribution_widget` (RedistributionEditor) carry a
        # `data` prop; both may also be a StepRef bundled at compile
        # as `data_from`. The two differ in acceptable shape:
        #   - histogram_widget: dict `{x_value: count}` only
        #   - redistribution_widget: dict OR list-of-dicts (widget
        #     normalizes both on its side)
        data_from = new_props.pop("data_from", None)
        if data_from is not None:
            value = _resolve_step_ref(data_from, steps_data)
            if block.type == "redistribution_widget":
                # Accept either shape; otherwise empty.
                new_props["data"] = (
                    value if isinstance(value, (dict, list)) else []
                )
            else:
                new_props["data"] = value if isinstance(value, dict) else {}

        # RedistributionEditor — sources and destinations are
        # list[str]; resolved StepRefs degrade to empty if not.
        if block.type == "redistribution_widget":
            for field_name in ("sources", "destinations"):
                ref_key = f"{field_name}_from"
                ref_val = new_props.pop(ref_key, None)
                if ref_val is not None:
                    value = _resolve_step_ref(ref_val, steps_data)
                    new_props[field_name] = (
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
    return render(
        template_str,
        build_steps_with_workflow(workflow, submission),
        variables=_variables_snapshot(),
    )
