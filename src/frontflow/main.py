"""
FastAPI app for the Workflow Runner backend.

Discovers form files from a configurable directory (WORKFLOWS_DIR),
executes and compiles each one in isolation, and serves them as forms
keyed by their `workflow_id`. A bad form file is skipped, not fatal;
new files are picked up via POST /refresh without a restart.

Endpoints (all paths spell out full words):

  Forms:
    GET  /forms                                     forms index + counts
    GET  /forms/{form_id}                           pre-submission schema
    GET  /forms/{form_id}/submissions               a form's submissions

  Submission lifecycle:
    POST /forms/{form_id}/submissions               start a new submission
    GET  /submissions/{submission_id}               full submission state
    POST /submissions/{submission_id}/clear         rewind state

  Step-level interactions:
    GET  /submissions/{submission_id}/steps/{step_id}   step schema + values
    POST /submissions/{submission_id}/steps/{step_id}   submit a step

  Service:
    GET  /                                          liveness + loaded forms
    POST /refresh                                   re-scan form files
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import secrets
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    FastAPI,
    Form,
    Header,
    HTTPException,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from frontflow.dsl import WORKFLOWS, store
from frontflow.dsl import auth
from frontflow.dsl import uploads
from frontflow.dsl.compile import (
    CompiledBackendStep,
    CompiledNode,
    CompiledPage,
    CompiledWorkflow,
    compile_workflow,
    serialize_workflow,
    workflow_content_hash,
)
from frontflow.dsl.runtime import (
    Submission,
    advance,
    build_steps_with_workflow,
    chain_is_real,
    clear_submission_from,
    external_task_states,
    get_submission,
    hydrate_submission,
    resolve_template,
    resolve_layout,
    start_submission,
    submission_snapshot,
    submit_step,
)
from frontflow.dsl.airflow_dispatch import respond_to_hitl
from frontflow.dsl.airflow_hook import AirflowError
from frontflow.dsl.runtime import _airflow_hook_for
from frontflow.dsl.templating import render
from frontflow.dsl.sources import workflow_source_from_uri


# --- Workflow loading ------------------------------------------------------
#
# Form files are discovered from a directory — WORKFLOWS_DIR, configurable
# so the service can read from a deploy location that CI/CD pushes to.
# Each file is executed in isolation; running it registers whatever @form
# workflows it defines, the same way Airflow discovers DAGs from its dags
# folder. A form's id is the workflow_id from its @form decorator (the
# function name, or an explicit `workflow_id=`) — never the filename.
#
# scan_workflows() is idempotent and safe to call repeatedly: form files
# pushed by CI/CD are picked up by POST /refresh, with no service restart.
# A file that fails to import, or a workflow that fails to compile, is
# recorded in LOAD_ERRORS and skipped — it never affects the other forms
# and never takes the service down.

# The workflow source — where workflow files are loaded from. Set from
# the WORKFLOW_SOURCE environment variable (a path or an s3:// URI);
# the CLI sets it from --source. Defaults to the bundled examples.
# WORKFLOWS_DIR is still honored for backward compatibility.
_source_uri = os.environ.get("WORKFLOW_SOURCE") or os.environ.get(
    "WORKFLOWS_DIR"
) or str(Path(__file__).parent / "examples")
WORKFLOW_SOURCE = workflow_source_from_uri(_source_uri)


# The bearer token guarding the submission export API. Set via the
# FRONTFLOW_API_TOKEN environment variable (or the --env-file). The
# export endpoint exposes submitted form data, so when no token is
# configured it refuses to serve rather than running open — a
# deployment can't accidentally expose submission data.
API_TOKEN = os.environ.get("FRONTFLOW_API_TOKEN") or None


def require_api_token(
    authorization: str | None = Header(default=None),
) -> None:
    """FastAPI dependency — enforce the export API's bearer token.

    503 if the server has no token configured (misconfiguration: the
    operator must set FRONTFLOW_API_TOKEN). 401 if the caller's token
    is missing or wrong.
    """
    if API_TOKEN is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "export API is disabled — set FRONTFLOW_API_TOKEN to "
                "enable it"
            ),
        )
    expected = f"Bearer {API_TOKEN}"
    # Constant-time compare so a wrong token can't be timing-probed.
    if authorization is None or not secrets.compare_digest(
        authorization, expected
    ):
        raise HTTPException(
            status_code=401,
            detail="missing or invalid API token",
        )


# The session cookie name for the admin console.
_SESSION_COOKIE = "frontflow_session"


def require_admin(
    frontflow_session: str | None = Cookie(default=None),
) -> "store.User":
    """FastAPI dependency — gate the admin console surface.

    503 if no account exists yet (bootstrap: run `frontflow
    create-admin`). 401 if the caller has no valid admin session.
    Returns the authenticated admin user.
    """
    if not auth.any_users_exist():
        raise HTTPException(
            status_code=503,
            detail=(
                "admin console is not configured — create an account "
                "with `frontflow create-admin`"
            ),
        )
    user = auth.resolve_session(frontflow_session)
    if user is None:
        raise HTTPException(
            status_code=401, detail="authentication required"
        )
    if not user.is_admin:
        raise HTTPException(
            status_code=403, detail="admin privileges required"
        )
    return user


def _current_user(
    frontflow_session: str | None = Cookie(default=None),
) -> "store.User":
    """FastAPI dependency — the signed-in user, or 401. Unlike
    require_admin this does not require admin; it is the base for the
    per-form access checks."""
    if not auth.any_users_exist():
        raise HTTPException(
            status_code=503,
            detail=(
                "the console is not configured — create an account "
                "with `frontflow create-admin`"
            ),
        )
    user = auth.resolve_session(frontflow_session)
    if user is None:
        raise HTTPException(
            status_code=401, detail="authentication required"
        )
    return user


def require_form_access(min_role: str):
    """Build a dependency that gates a per-form route by folder grant.

    `min_role` is 'view' or 'manage'. The user must be signed in and
    their resolved access to the form (admin, or a folder grant) must
    meet `min_role`. 404 if the form does not exist; 403 if the user
    lacks sufficient access.
    """
    rank = {"view": 1, "manage": 2}

    def _dep(
        form_id: str,
        user: "store.User" = Depends(_current_user),
    ) -> "store.User":
        folder = store.form_folder(form_id)
        if folder is None:
            raise HTTPException(
                status_code=404, detail=f"form {form_id!r} not found"
            )
        access = auth.user_form_access(user, folder)
        if access is None or rank[access] < rank[min_role]:
            raise HTTPException(
                status_code=403,
                detail=f"{min_role} access to this form is required",
            )
        return user

    return _dep


def require_form_visibility(
    form_id: str,
    frontflow_session: str | None = Cookie(default=None),
    key: str | None = None,
) -> None:
    """FastAPI dependency — gate the form-FILLING surface by the form's
    visibility. `key` is the unlisted-link token (query param).

    public reaches everyone; unlisted needs the token; restricted needs
    a permitted signed-in user; a folder grant or admin always passes.
    An unauthorized visitor gets 404 — a restricted or unlisted form's
    existence is not leaked by URL probing.
    """
    user = auth.resolve_session(frontflow_session)
    if not auth.can_access_form(user, form_id, key):
        raise HTTPException(
            status_code=404, detail=f"form {form_id!r} not found"
        )

# The live registry, keyed by form_id. Rebound wholesale by each scan so
# in-flight requests always read a complete, consistent snapshot.
FORMS: dict[str, CompiledWorkflow] = {}

# Per-file / per-workflow load failures from the most recent scan.
LOAD_ERRORS: dict[str, str] = {}

# form_id -> the id of the form_version that is currently live (the
# latest compiled state). New submissions are pinned to this.
FORM_VERSION_IDS: dict[str, int] = {}

# form_version id -> its executable CompiledWorkflow. The live versions
# are seeded from FORMS; older versions are recompiled on demand from
# their stored DSL source so an in-flight submission always advances on
# the version it began. An old version's compiled form never changes,
# so cache entries stay valid across rescans.
_VERSION_WF_CACHE: dict[int, CompiledWorkflow] = {}
_compile_lock = threading.Lock()


def _exec_form_source(name: str, source: str) -> None:
    """Execute one workflow file's source text. Running the module body
    registers its @form workflows into WORKFLOWS via the decorator's
    trailing call. `name` is the file's source-relative name, used to
    derive a unique module name."""
    mod_name = _mod_name(name)
    module = importlib.util.module_from_spec(
        importlib.util.spec_from_loader(mod_name, loader=None)
    )
    module.__file__ = name
    sys.modules[mod_name] = module  # registered before exec for self-refs
    code = compile(source, filename=name, mode="exec")
    exec(code, module.__dict__)  # noqa: S102 — workflow files are code


def _mod_name(name: str) -> str:
    """A unique module name from a workflow file's source-relative name
    — so files with the same stem in different folders don't collide in
    sys.modules."""
    stem = name[:-3] if name.endswith(".py") else name
    return "_forms." + ".".join(stem.split("/"))


def compile_source(source: str, form_id: str) -> CompiledWorkflow:
    """Recompile a workflow from stored DSL source — used to reconstruct
    an executable graph for a form_version that isn't the live one.

    The DSL's @form decorator registers into the global WORKFLOWS dict;
    this temporarily clears it, executes the source, compiles the named
    workflow, and restores the registry."""
    with _compile_lock:
        snapshot = dict(WORKFLOWS)
        WORKFLOWS.clear()
        try:
            namespace: dict[str, Any] = {}
            exec(  # noqa: S102 — stored DSL source, same trust as a form file
                compile(source, f"<form_version:{form_id}>", "exec"),
                namespace,
            )
            wf = WORKFLOWS.get(form_id)
            if wf is None:
                raise RuntimeError(
                    f"stored source for {form_id!r} did not register it"
                )
            return compile_workflow(wf)
        finally:
            WORKFLOWS.clear()
            WORKFLOWS.update(snapshot)


def resolve_workflow(form_version_id: int) -> CompiledWorkflow:
    """The executable CompiledWorkflow for a form_version. Live versions
    come straight from FORMS; older ones are recompiled from their
    stored source. Cached — recompilation happens at most once."""
    cached = _VERSION_WF_CACHE.get(form_version_id)
    if cached is not None:
        return cached
    fv = store.get_form_version(form_version_id)
    if fv is None:
        raise KeyError(f"form_version {form_version_id} not found")
    form_id = fv["form_id"]
    if FORM_VERSION_IDS.get(form_id) == form_version_id and form_id in FORMS:
        wf = FORMS[form_id]
    else:
        wf = compile_source(fv["source"], form_id)
    _VERSION_WF_CACHE[form_version_id] = wf
    return wf


def scan_workflows() -> dict[str, CompiledWorkflow]:
    """(Re)discover, execute, and compile every form file under
    WORKFLOWS_DIR (recursing into subfolders), and record each form's
    current compiled state as a form_version.

    Idempotent — clears and rebuilds the registry — so it runs at startup
    and again whenever form files change. Returns the new FORMS registry.
    """
    global FORMS, LOAD_ERRORS, FORM_VERSION_IDS
    store.init_db()
    errors: dict[str, str] = {}

    # Fresh registration pass: clearing first means re-running a file
    # doesn't trip the @form "already registered" guard.
    WORKFLOWS.clear()

    # form_id -> (folder_path, dsl_source) — captured per file so each
    # form_version stores the folder it lives in and the source it was
    # compiled from.
    form_meta: dict[str, tuple[str, str]] = {}

    try:
        workflow_files = list(WORKFLOW_SOURCE.iter_files())
    except Exception as e:  # noqa: BLE001 — a bad source != a crash
        print(
            f"[workflow] could not read workflow source "
            f"({WORKFLOW_SOURCE.describe()}): {e}"
        )
        FORMS, LOAD_ERRORS, FORM_VERSION_IDS = {}, {"_source": str(e)}, {}
        return FORMS

    for wf_file in workflow_files:
        before = set(WORKFLOWS)
        try:
            _exec_form_source(wf_file.name, wf_file.source)
        except Exception as e:  # noqa: BLE001 — isolate: one bad file != outage
            errors[wf_file.name] = (
                f"import failed — {type(e).__name__}: {e}"
            )
            print(f"[workflow] {wf_file.name}: import failed — {e}")
            traceback.print_exc()
            continue
        for new_id in set(WORKFLOWS) - before:
            form_meta[new_id] = (wf_file.folder, wf_file.source)

    compiled: dict[str, CompiledWorkflow] = {}
    version_ids: dict[str, int] = {}
    for wf_id, wf in list(WORKFLOWS.items()):
        try:
            cw = compile_workflow(wf)
        except Exception as e:  # noqa: BLE001 — isolate per workflow
            errors[wf_id] = f"compile failed — {type(e).__name__}: {e}"
            print(f"[workflow] {wf_id}: compile failed — {e}")
            continue
        compiled[wf_id] = cw
        folder, source = form_meta.get(wf_id, ("", ""))
        try:
            serialized = serialize_workflow(cw)
            vid = store.upsert_form_version(
                form_id=wf_id,
                name=cw.title or wf_id,
                folder_path=folder,
                compiled_graph=serialized,
                content_hash=workflow_content_hash(serialized),
                source=source,
            )
            version_ids[wf_id] = vid
            _VERSION_WF_CACHE[vid] = cw  # seed the live version's cache
        except Exception as e:  # noqa: BLE001 — persistence must not break serving
            print(f"[workflow] {wf_id}: version persistence failed — {e}")

    store.mark_forms_live(set(compiled))
    FORMS, LOAD_ERRORS, FORM_VERSION_IDS = compiled, errors, version_ids
    summary = f"[workflow] serving forms: {sorted(FORMS)}"
    if LOAD_ERRORS:
        summary += f" | load errors: {sorted(LOAD_ERRORS)}"
    print(summary)
    return FORMS


def hydrate_state() -> None:
    """Rehydrate persisted submissions into the runtime's in-memory
    working set. Run once at startup, after the workflow scan."""
    loaded = 0
    for snap in store.load_submissions():
        try:
            wf = resolve_workflow(snap["form_version_id"])
        except Exception as e:  # noqa: BLE001 — skip what can't be resolved
            print(
                f"[workflow] submission {snap['handle']}: "
                f"cannot resolve form version — {e}"
            )
            continue
        hydrate_submission(snap, form_id=wf.id)
        loaded += 1
    if loaded:
        print(f"[workflow] rehydrated {loaded} submission(s)")


# Form discovery and submission rehydration run on application
# *startup*, not at import time — importing this module must only
# define the app. Scanning at import is re-entrant (a workflow file
# imports `frontflow`) and fragile under tooling that imports the
# module to inspect it.


# --- App setup -------------------------------------------------------------


app = FastAPI(title="frontflow")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Every data route is registered on this router and mounted under
# `/api`, so API paths never collide with the single-page-app's own
# page paths (e.g. the SPA page `/forms` vs the API `GET /api/forms`).
api = APIRouter()


@app.on_event("startup")
def _on_startup() -> None:
    """Discover workflow files and rehydrate persisted submissions —
    once, when the server starts. Not at import time."""
    scan_workflows()
    hydrate_state()


# --- Request / response models ---------------------------------------------


class StartSubmissionRequest(BaseModel):
    # The initial form values that will be auto-submitted for the
    # workflow's first step. The frontend's LandingPage posts this with
    # whatever fields the first step defines.
    values: dict[str, Any] = Field(default_factory=dict)


class HitlPrompt(BaseModel):
    """The human-in-the-loop action an Airflow HITL task is waiting on —
    what the form renders so the user can respond."""
    subject: Optional[str] = None
    body: Optional[str] = None
    options: list[str] = []
    # Airflow's param schema for the action — rendered as extra fields.
    params: dict[str, Any] = {}
    defaults: list[str] = []
    # Whether more than one option may be chosen.
    multiple: bool = False


class TaskInstance(BaseModel):
    # One row in the submission's chain.
    #   kind="hitl"     — a HITL node (a form screen)
    #   kind="external" — an ExternalTask (Airflow poll, etc.)
    #   kind="backend"  — a workflow-level @backend step
    task_id: str
    state: str
    is_hitl: bool
    kind: str = "hitl"
    # Edit-cascade status — "unaffected" | "needs_review" | "needs_input".
    # How an upstream edit left this step; "unaffected" for steps no
    # edit has touched.
    status: str = "unaffected"
    # The page this task belongs to, when it's a node inside a page (or
    # an external task trailing such a node). Null for top-level nodes
    # and backend steps. The frontend groups the chain by page.
    page_id: Optional[str] = None
    page_title: Optional[str] = None
    # Present only on an Airflow HITL task in `awaiting_response` — the
    # prompt the form renders to collect the user's answer.
    hitl: Optional[HitlPrompt] = None
    # A human-readable note about this task — for an Airflow operator,
    # the failure reason or a short status message. Null when there's
    # nothing to say.
    detail: Optional[str] = None
    # Whether a user may rerun *this step on its own* from the UI.
    # False suppresses the per-step rerun menu; an upstream cascade can
    # still clear the step. HITL/form nodes are always retryable.
    retryable: bool = True


class SubmissionResponse(BaseModel):
    # `handle` is the stable key the client addresses the submission by
    # while it's still a draft. `submission_id` is the minted, canonical
    # id — null until its source value is available; once set, the
    # submission is resumable at /submissions/{submission_id}.
    handle: str
    submission_id: Optional[str] = None
    form_id: str
    state: str
    started_at: str
    tasks: list[TaskInstance]


class Block(BaseModel):
    """One node in the layout tree. Shipped to the frontend as a nested
    tree and rendered by a recursive component registry — containers,
    display blocks, inputs, and buttons all use this uniform shape."""
    type: str
    id: Optional[str] = None
    props: dict[str, Any] = Field(default_factory=dict)
    children: list["Block"] = Field(default_factory=list)


class StepDetail(BaseModel):
    # `handle` addresses the submission while it's a draft; once minted,
    # `submission_id` is the canonical id (null until then).
    handle: str
    submission_id: Optional[str] = None
    step_id: str
    # The layout tree the frontend renders.
    layout: Block
    response_received: bool
    # When submitted: {"values": {...}, "button": "<button_id>"}.
    response: Optional[dict[str, Any]] = None
    # When the step is open but carries answers from a prior submission
    # — i.e. it was re-opened: {"values": {...}, "button": "<id>"}.
    # Null for a blank (never-filled or reset) step. The frontend seeds
    # the form with these values.
    draft: Optional[dict[str, Any]] = None
    # Edit-cascade status of this step — "unaffected" | "needs_review"
    # | "needs_input".
    status: str = "unaffected"
    # True when this step is the node the user is actively editing
    # (re-opened via "edit", not yet re-submitted). The frontend shows
    # a Cancel affordance only then — Cancel is a clean inverse only
    # before the cascade has run.
    edit_in_progress: bool = False
    responded_at: Optional[str] = None
    # True for the workflow's landing step. The frontend uses this to
    # suppress the per-step reset affordance — the landing step sets
    # the submission id, so its values must not be individually
    # rewound. A full-submission reset preserves them.
    is_landing: bool = False
    # The page this step belongs to, when it's a section node inside a
    # page. Null for top-level nodes. The frontend uses these to render
    # the page as its own view.
    page_id: Optional[str] = None
    page_title: Optional[str] = None


class FormLandingStep(BaseModel):
    """The landing step's schema — the layout tree the frontend renders
    on the form's landing page."""
    step_id: str
    layout: Block


class ThemeColors(BaseModel):
    bg: str
    surface: str
    ink: str
    muted: str
    border: str
    accent: str
    accentHover: str
    error: str


class ThemeFonts(BaseModel):
    sans: str
    display: str
    mono: str
    googleFontsHref: Optional[str] = None


class ThemeDisplay(BaseModel):
    transform: str  # none | uppercase | lowercase
    tracking: str
    style: str  # normal | italic


class ThemeGeometry(BaseModel):
    radius: str
    nodeGap: str
    scrollHeadroom: str


class HeaderStyle(BaseModel):
    size: str
    weight: int
    color: str


class ThemeHeaders(BaseModel):
    h1: HeaderStyle
    h2: HeaderStyle
    h3: HeaderStyle
    h4: HeaderStyle


class EmphasisBold(BaseModel):
    color: str
    weight: int


class EmphasisColor(BaseModel):
    color: str


class ThemeEmphasis(BaseModel):
    bold: EmphasisBold
    italic: EmphasisColor
    underline: EmphasisColor


class ThemeFormTitle(BaseModel):
    color: str


class ThemeEffects(BaseModel):
    grain: bool = False


class FormTheme(BaseModel):
    """A form's theme — the full token set the form-facing views render
    with. Mirrors the frontend `Theme` interface; stored as JSON on the
    form row."""
    name: str = "Custom"
    colors: ThemeColors
    fonts: ThemeFonts
    display: ThemeDisplay
    geometry: ThemeGeometry
    headers: ThemeHeaders
    emphasis: ThemeEmphasis
    formTitle: ThemeFormTitle
    effects: ThemeEffects


class FormDetail(BaseModel):
    """Pre-submission form metadata returned by GET /forms/{form_id}."""
    form_id: str
    title: str
    description: str
    landing_step: FormLandingStep
    # The form's custom theme, or null when uncustomized (the frontend
    # falls back to its default product theme).
    theme: Optional[FormTheme] = None


class StepSubmissionRequest(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    # Which button the user clicked. Optional for single-button steps
    # (the runtime defaults to the only button).
    button: Optional[str] = None


class ClearRequest(BaseModel):
    from_task_id: Optional[str] = None
    dry_run: bool = False
    # "reset" re-opens the target step empty; "edit" re-opens it with
    # its previously-submitted answers carried over as a draft.
    mode: Literal["reset", "edit"] = "reset"
    # For an edit: "cascade" runs the dependency-aware cascade on
    # re-submit; "node_only" re-submits the edited step alone, leaving
    # every downstream step exactly as it was.
    scope: Literal["cascade", "node_only"] = "cascade"


class ClearResponse(BaseModel):
    affected_tasks: list[str]
    cleared: bool


# --- Helpers ---------------------------------------------------------------


def _get_form_or_404(form_id: str) -> CompiledWorkflow:
    form = FORMS.get(form_id)
    if form is None:
        raise HTTPException(status_code=404, detail=f"form {form_id!r} not found")
    return form


def _to_block(cb: Any) -> Block:
    """Convert a CompiledBlock (from the compiler) into the Block
    response model — recursively."""
    return Block(
        type=cb.type,
        id=cb.id,
        props=cb.props,
        children=[_to_block(c) for c in cb.children],
    )


def _get_submission_or_404(
    form_id: str, submission_id: str
) -> tuple[CompiledWorkflow, Submission]:
    """Resolve `(form, submission)`, validating that the submission
    actually belongs to the form named in the URL. A mismatch returns
    404 (not 403) — from the client's perspective, the submission
    doesn't exist under that form's namespace.

    The returned workflow is the *submission's own form version* — an
    in-flight submission always advances and renders on the version it
    began, even if the live DSL has since changed.
    """
    _get_form_or_404(form_id)
    submission = get_submission(submission_id)
    if submission is None:
        raise HTTPException(status_code=404, detail="submission not found")
    if submission.form_id != form_id:
        raise HTTPException(
            status_code=404,
            detail=(
                f"submission {submission_id!r} does not belong to form "
                f"{form_id!r} (belongs to {submission.form_id!r})"
            ),
        )
    try:
        form = resolve_workflow(submission.form_version_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"cannot load this submission's form version: {e}",
        ) from e
    return form, submission


def _persist(workflow: CompiledWorkflow, submission: Submission) -> None:
    """Write the submission's current state through to the database.

    A no-op while the submission is still a session draft — persistence
    begins the moment its id is minted. Called after every runtime
    operation; failures are logged, never surfaced to the request."""
    if submission.submission_id is None:
        return
    try:
        store.sync_submission(submission_snapshot(workflow, submission))
    except Exception as e:  # noqa: BLE001 — persistence must not break a request
        print(f"[workflow] persist failed for {submission.handle}: {e}")
        traceback.print_exc()


def _submission_state(submission: Submission) -> str:
    """Translate submission state into the string the frontend expects:
    queued | running | success | failed."""
    if submission.failed:
        return "failed"
    if submission.terminated:
        return "success"
    if not submission.steps:
        return "queued"
    latest = submission.steps[-1]
    if not latest.is_submitted and len(submission.steps) == 1:
        return "queued"
    return "running"


def _build_tasks(
    submission: Submission, form: CompiledWorkflow
) -> list[TaskInstance]:
    """Flatten the submission's state into the task list the frontend's
    chain-segmentation logic consumes.

    Each step becomes a task: HITL nodes → kind="hitl", backend steps →
    kind="backend" (hidden backend steps are omitted entirely),
    ExternalTask operators → kind="external" with state derived from
    elapsed time since the form was submitted.
    """
    tasks: list[TaskInstance] = []
    for step in submission.steps:
        step_def = form.all_nodes_by_id.get(step.node_id) or form.by_id.get(
            step.node_id
        )

        if isinstance(step_def, CompiledBackendStep):
            # Hidden steps are suppressed — but only when they succeed.
            # A failure always surfaces, so it has somewhere to show.
            if step_def.hidden and not step.error:
                continue
            if step.error:
                state = "failed"
            elif step.is_submitted:
                state = "success"
            else:
                state = "running"
            tasks.append(
                TaskInstance(
                    task_id=step.node_id,
                    state=state,
                    is_hitl=False,
                    kind="backend",
                    status=step.status.key,
                    retryable=step_def.fn.retryable,
                )
            )
            continue

        # A node — top-level or a page section node.
        page = form.node_page.get(step.node_id)
        page_id = page.id if page is not None else None
        page_title = page.title if page is not None else None

        if step.error:
            form_state = "failed"
        elif step.is_submitted:
            form_state = "success"
        else:
            form_state = "deferred"
        tasks.append(
            TaskInstance(
                task_id=step.node_id,
                state=form_state,
                is_hitl=True,
                kind="hitl",
                status=step.status.key,
                page_id=page_id,
                page_title=page_title,
            )
        )

        if step.is_submitted and not step.branch_taken_explicitly:
            ext_tasks = step_def.external_tasks
            if chain_is_real(ext_tasks):
                # Connected Airflow operators — show their polled state.
                for ext in ext_tasks:
                    st = step.external_state.get(ext.task_id)
                    if st is None:
                        break  # not reached yet
                    state = st.get("state", "queued")
                    # An Airflow HITL task that is waiting on a person
                    # carries the prompt for the form to render.
                    prompt = None
                    if state == "awaiting_response" and st.get("hitl"):
                        prompt = HitlPrompt(**st["hitl"])
                    tasks.append(
                        TaskInstance(
                            task_id=ext.task_id,
                            state=state,
                            is_hitl=ext.kind in (
                                "airflow_hitl", "airflow_hitl_branch"
                            ),
                            kind="external",
                            page_id=page_id,
                            page_title=page_title,
                            hitl=prompt,
                            detail=st.get("detail"),
                            retryable=ext.retryable,
                        )
                    )
                    if state != "success":
                        break  # nothing after a non-terminal task
            else:
                # Legacy / connectionless tasks — mock progression.
                elapsed = (
                    datetime.now(timezone.utc) - step.submitted_at
                ).total_seconds()
                visible = external_task_states(
                    len(ext_tasks), elapsed
                )
                for idx, state in visible:
                    ext = ext_tasks[idx]
                    tasks.append(
                        TaskInstance(
                            task_id=ext.task_id,
                            state=state,
                            is_hitl=False,
                            kind="external",
                            page_id=page_id,
                            page_title=page_title,
                            retryable=ext.retryable,
                        )
                    )
    return tasks


def _build_submission_response(
    submission: Submission, form: CompiledWorkflow
) -> SubmissionResponse:
    return SubmissionResponse(
        handle=submission.handle,
        submission_id=submission.submission_id,
        form_id=submission.form_id,
        state=_submission_state(submission),
        started_at=submission.started_at.isoformat(),
        tasks=_build_tasks(submission, form),
    )


def _value_is_blank(field_type: str, value: Any) -> bool:
    """Whether a submitted value counts as empty for a required-field
    check. Each input type carries its value differently — a checkbox
    is a bool, the ranges are dicts, multi-select is a list — so a
    plain string check isn't enough."""
    if value is None:
        return True
    if field_type == "checkbox":
        return value is not True
    if field_type == "multi_select":
        return not isinstance(value, list) or len(value) == 0
    if field_type == "checkbox_grid":
        if not isinstance(value, dict):
            return True
        return not any(
            isinstance(cols, list) and cols for cols in value.values()
        )
    if field_type == "date_range":
        return not (
            isinstance(value, dict)
            and str(value.get("start", "")).strip()
            and str(value.get("end", "")).strip()
        )
    if field_type == "number_range":
        return not (
            isinstance(value, dict)
            and value.get("min") is not None
            and value.get("max") is not None
        )
    if field_type in ("rating", "slider", "number"):
        # Numeric inputs — blank unless a real, finite number. A rating
        # of 0 means "unrated"; the scale is 1..max.
        if not isinstance(value, (int, float)) or isinstance(
            value, bool
        ):
            return True
        if field_type == "rating":
            return value < 1
        return False
    if field_type in ("file", "s3file"):
        # A file value is the upload reference dict; blank unless it
        # carries an actual upload (a token, or an S3 bucket/key).
        if not isinstance(value, dict):
            return True
        return not (
            value.get("token") or (value.get("bucket") and value.get("key"))
        )
    if field_type == "sankey":
        # A list of connection triples — blank when no connection was
        # drawn.
        return not isinstance(value, list) or len(value) == 0
    # text, select, radio, date, textarea, email, tel, url, time, widget
    return not str(value).strip()


def _evaluate_condition(
    cond: dict[str, Any],
    values: dict[str, Any],
    steps_data: dict[str, Any],
) -> bool:
    """Evaluate one serialized field condition. A same-node condition
    tests the current node's `values`; a cross-node condition (one
    carrying a `node`) tests that upstream node's submitted value in
    `steps_data`. Mirrors the frontend / runtime evaluators."""
    if cond.get("node") is not None:
        node_data = steps_data.get(cond["node"])
        actual = (
            node_data.get(cond["field"])
            if isinstance(node_data, dict)
            else None
        )
    else:
        actual = values.get(cond["field"])
    op = cond["op"]
    operand = cond.get("value")
    if op == "equals":
        return actual == operand
    if op == "not_equals":
        return actual != operand
    if op == "in":
        return isinstance(operand, list) and actual in operand
    if op == "not_in":
        return isinstance(operand, list) and actual not in operand
    if op == "truthy":
        return bool(actual)
    if op == "falsy":
        return not bool(actual)
    return True


def _field_is_active(
    conditions: list[dict[str, Any]],
    values: dict[str, Any],
    steps_data: dict[str, Any],
) -> bool:
    """A field is active (visible) when every condition from its
    enclosing `When` blocks holds. No conditions → always active."""
    return all(_evaluate_condition(c, values, steps_data) for c in conditions)


def _prune_inactive(
    fields: list[Any],
    values: dict[str, Any],
    steps_data: dict[str, Any],
) -> dict[str, Any]:
    """Drop the values of fields whose visibility conditions aren't met,
    so a stale value from a hidden `When` never gets stored or passed
    to a @backend function."""
    inactive = {
        f.name
        for f in fields
        if f.conditions
        and not _field_is_active(f.conditions, values, steps_data)
    }
    return {k: v for k, v in values.items() if k not in inactive}


def _validate_required_fields(
    node: Any,
    values: dict[str, Any],
    steps_data: dict[str, Any],
) -> None:
    """Verify a node's submitted values satisfy its required fields.
    Raises HTTPException(400) listing any that are missing. Generic
    over any node — a required field inside an unsatisfied `When` is
    skipped, since a hidden field cannot be missing. `steps_data` is
    empty for the landing node and the submission's upstream data for
    a later step."""
    missing = [
        f.name
        for f in node.fields
        if f.required
        and _field_is_active(f.conditions, values, steps_data)
        and _value_is_blank(f.type, values.get(f.name))
    ]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"missing required field(s): {', '.join(missing)}",
        )


def _validate_landing_values(
    form: CompiledWorkflow, values: dict[str, Any]
) -> None:
    """Verify the request body satisfies the landing step's required
    fields. The landing node is the workflow's entry, so it has no
    upstream — its conditions are all same-node (empty `steps_data`)."""
    _validate_required_fields(form.landing_node(), values, {})


# --- Endpoints -------------------------------------------------------------


@api.get("/forms/{form_id}", response_model=FormDetail, dependencies=[Depends(require_form_visibility)])
def read_form(form_id: str) -> FormDetail:
    """Return the form's pre-submission schema — the layout tree the
    landing page renders, plus the form's title and description (from
    the @form / @landing definitions)."""
    form = _get_form_or_404(form_id)
    landing = form.landing_node()
    stored_theme = store.get_form_theme(form_id)
    return FormDetail(
        form_id=form.id,
        title=form.title,
        description=form.description,
        landing_step=FormLandingStep(
            step_id=landing.id,
            layout=_to_block(landing.layout),
        ),
        theme=FormTheme(**stored_theme) if stored_theme else None,
    )


@api.get("/forms/{form_id}/theme")
def read_form_theme(form_id: str) -> Optional[FormTheme]:
    """A form's custom theme, or null when it hasn't been customized."""
    _get_form_or_404(form_id)
    stored = store.get_form_theme(form_id)
    return FormTheme(**stored) if stored else None


@api.put("/forms/{form_id}/theme", response_model=FormTheme, dependencies=[Depends(require_form_access("manage"))])
def write_form_theme(form_id: str, theme: FormTheme) -> FormTheme:
    """Save a form's theme. The body is validated against the full
    token schema; contrast is not enforced."""
    _get_form_or_404(form_id)
    if not store.set_form_theme(form_id, theme.model_dump()):
        raise HTTPException(status_code=404, detail="form not found")
    return theme


@api.delete("/forms/{form_id}/theme", dependencies=[Depends(require_form_access("manage"))])
def clear_form_theme(form_id: str) -> dict[str, bool]:
    """Clear a form's custom theme — it reverts to the default."""
    _get_form_or_404(form_id)
    store.set_form_theme(form_id, None)
    return {"cleared": True}


# --- Form visibility (M3) --------------------------------------------------
#
# Who may reach a form's filling surface — public / unlisted /
# restricted. Changing it needs 'manage' on the form.


class VisibilityChange(BaseModel):
    visibility: str  # 'public' | 'unlisted' | 'restricted'


class AclChange(BaseModel):
    user_id: int


@api.get(
    "/forms/{form_id}/visibility",
    dependencies=[Depends(require_form_access("manage"))],
)
def get_visibility(form_id: str) -> dict[str, Any]:
    """A form's visibility mode, unlisted token, and restricted ACL."""
    v = auth.get_form_visibility(form_id)
    if v is None:
        raise HTTPException(
            status_code=404, detail=f"form {form_id!r} not found"
        )
    return v


@api.put(
    "/forms/{form_id}/visibility",
    dependencies=[Depends(require_form_access("manage"))],
)
def set_visibility(
    form_id: str, req: VisibilityChange
) -> dict[str, Any]:
    """Set a form's visibility mode."""
    try:
        auth.set_form_visibility(form_id, req.visibility)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return auth.get_form_visibility(form_id)


@api.post(
    "/forms/{form_id}/unlisted-token/regenerate",
    dependencies=[Depends(require_form_access("manage"))],
)
def regenerate_token(form_id: str) -> dict[str, str]:
    """Mint a fresh unlisted token — old links stop working."""
    try:
        token = auth.regenerate_unlisted_token(form_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"unlisted_token": token}


@api.post(
    "/forms/{form_id}/acl",
    dependencies=[Depends(require_form_access("manage"))],
)
def add_acl(form_id: str, req: AclChange) -> dict[str, bool]:
    """Permit a user on a restricted form."""
    try:
        auth.add_form_acl(form_id, req.user_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@api.delete(
    "/forms/{form_id}/acl/{user_id}",
    dependencies=[Depends(require_form_access("manage"))],
)
def remove_acl(form_id: str, user_id: int) -> dict[str, bool]:
    """Remove a user from a restricted form's allow-list."""
    auth.remove_form_acl(form_id, user_id)
    return {"ok": True}


# --- Connection store ------------------------------------------------------
#
# Stored credentialed endpoints — currently Airflow instances — that
# workflow operators authenticate against by name. Credentials are
# encrypted at rest (workflows/crypto.py); the API never returns them.


class ConnectionSummary(BaseModel):
    """A stored connection as the API exposes it — metadata only. The
    credentials are deliberately never serialized back."""
    name: str
    conn_type: str
    base_url: str
    auth_kind: str
    created_at: datetime
    updated_at: datetime


class ConnectionInput(BaseModel):
    """Create/update payload. Credentials are write-only: supply them to
    set or rotate, omit them on an update to keep the stored ones. The
    relevant fields depend on `auth_kind` — username/password for
    'basic', token for 'token', the AWS key set for 'aws'."""
    conn_type: str = "airflow"
    base_url: str = ""
    auth_kind: str
    username: Optional[str] = None
    password: Optional[str] = None
    token: Optional[str] = None
    # AWS credential fields — used when auth_kind == "aws".
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    aws_session_token: Optional[str] = None
    aws_region: Optional[str] = None
    aws_bucket: Optional[str] = None


def _connection_summary(rec: dict) -> ConnectionSummary:
    return ConnectionSummary(
        name=rec["name"],
        conn_type=rec["conn_type"],
        base_url=rec["base_url"],
        auth_kind=rec["auth_kind"],
        created_at=rec["created_at"],
        updated_at=rec["updated_at"],
    )


@api.get("/connections", response_model=list[ConnectionSummary], dependencies=[Depends(require_admin)])
def list_connections() -> list[ConnectionSummary]:
    """Every stored connection, metadata only."""
    return [_connection_summary(c) for c in store.list_connections()]


@api.get("/connections/{name}", response_model=ConnectionSummary, dependencies=[Depends(require_admin)])
def read_connection(name: str) -> ConnectionSummary:
    """One connection's metadata. Credentials are never returned — the
    editor re-enters them to rotate."""
    rec = store.get_connection(name)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"connection {name!r} not found")
    return _connection_summary(rec)


@api.put("/connections/{name}", response_model=ConnectionSummary, dependencies=[Depends(require_admin)])
def write_connection(name: str, body: ConnectionInput) -> ConnectionSummary:
    """Create or update a connection. Omitting credentials on an update
    keeps the stored ones; a new connection — or a change of auth kind —
    requires them."""
    if body.auth_kind not in ("basic", "token", "aws"):
        raise HTTPException(
            status_code=422,
            detail="auth_kind must be 'basic', 'token', or 'aws'",
        )

    # Assemble the credential payload from whichever fields were given.
    secret: Optional[dict[str, str]] = None
    if body.auth_kind == "basic":
        if body.username is not None or body.password is not None:
            if not body.username or not body.password:
                raise HTTPException(
                    status_code=422,
                    detail="basic auth needs both username and password",
                )
            secret = {"username": body.username, "password": body.password}
    elif body.auth_kind == "token":
        if body.token is not None:
            if not body.token:
                raise HTTPException(
                    status_code=422, detail="token auth needs a non-empty token"
                )
            secret = {"token": body.token}
    else:  # aws
        if (
            body.aws_access_key_id is not None
            or body.aws_secret_access_key is not None
        ):
            if not body.aws_access_key_id or not body.aws_secret_access_key:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "aws auth needs both an access key id and a "
                        "secret access key"
                    ),
                )
            secret = {
                "aws_access_key_id": body.aws_access_key_id,
                "aws_secret_access_key": body.aws_secret_access_key,
            }
            # Optional members — only stored when supplied.
            if body.aws_session_token:
                secret["aws_session_token"] = body.aws_session_token
            if body.aws_region:
                secret["region"] = body.aws_region
            if body.aws_bucket:
                secret["bucket"] = body.aws_bucket

    existing = next(
        (c for c in store.list_connections() if c["name"] == name), None
    )
    if existing is None and secret is None:
        raise HTTPException(
            status_code=422, detail="a new connection requires credentials"
        )
    if (
        existing is not None
        and secret is None
        and existing["auth_kind"] != body.auth_kind
    ):
        raise HTTPException(
            status_code=422, detail="changing auth kind requires new credentials"
        )

    store.upsert_connection(
        name=name,
        conn_type=body.conn_type,
        base_url=body.base_url,
        auth_kind=body.auth_kind,
        secret=secret,
    )
    return _connection_summary(store.get_connection(name))


@api.delete("/connections/{name}", dependencies=[Depends(require_admin)])
def remove_connection(name: str) -> dict[str, bool]:
    """Delete a connection."""
    if not store.delete_connection(name):
        raise HTTPException(status_code=404, detail=f"connection {name!r} not found")
    return {"deleted": True}


# --- Workflow structural graph ---------------------------------------------
#
# GET /forms/{form_id}/graph returns the compiled workflow as a flat
# node/edge graph. Every input, node-internal @backend, external task,
# and workflow-level backend step is a node; node-groups are containers.
# Edges are typed: in-group flow (inputs → backend → external tasks),
# execution `>>` between top-level steps, and input-level `steps.X.Y`
# data dependencies. The form summary page renders this as a DAG.


class GraphGroup(BaseModel):
    """A node-group — a container enclosing its member nodes."""
    id: str
    title: str
    page_id: Optional[str] = None
    is_landing: bool = False
    # The node submits via an @backend.branch.
    is_branch: bool = False


class GraphNode(BaseModel):
    id: str
    # "input" | "backend" (node-internal @backend) | "airflow" (a
    # graph-visible Airflow operator) | "workflow_backend" (a standalone
    # workflow-level backend step).
    kind: str
    # The node-group this belongs to; null for a workflow-level step.
    group_id: Optional[str] = None
    label: str
    # Input type, external-task kind, or "branch" for a branch backend.
    detail: Optional[str] = None
    # In-group rank: inputs 0, node-internal backend 1, external tasks
    # 2, 3, … — the within-group sub-DAG flows along this.
    rank: int = 0
    is_branch: bool = False
    required: bool = False


class GraphEdge(BaseModel):
    id: str
    from_node: str
    to_node: str
    # "in_group" — inputs → backend → tasks, inside one group;
    # "execution" — the `>>` flow between top-level steps;
    # "dependency" — a `steps.X.Y` data reference.
    relation: str
    # For a dependency edge: the reference site and its consequence.
    dep_source: Optional[str] = None  # options|default|condition|argument
    dep_kind: Optional[str] = None  # functional|display


class WorkflowGraph(BaseModel):
    form_id: str
    title: str
    groups: list[GraphGroup]
    nodes: list[GraphNode]
    edges: list[GraphEdge]


def _entry_step_id(form: CompiledWorkflow, target_id: str) -> Optional[str]:
    """Resolve a `>>` target id to the concrete step the flow enters —
    a page resolves to its entry section node."""
    step = form.by_id.get(target_id)
    if isinstance(step, CompiledPage):
        return step.entry_node_id
    if step is not None:
        return target_id
    return target_id if target_id in form.all_nodes_by_id else None


def _exit_step_ids(form: CompiledWorkflow, step_id: str) -> list[str]:
    """The concrete step ids a `>>` edge out of `step_id` originates
    from — a page exits from each terminal section node."""
    step = form.by_id.get(step_id)
    if isinstance(step, CompiledPage):
        return list(step.terminal_node_ids)
    return [step_id]


def _build_workflow_graph(form: CompiledWorkflow) -> WorkflowGraph:
    groups: list[GraphGroup] = []
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []

    # Per-node lookups for resolving dependency endpoints.
    node_input_ids: dict[str, set[str]] = {}
    node_backend_fn: dict[str, Optional[str]] = {}

    def backend_node_id(node_id: str) -> str:
        return f"{node_id}#backend"

    def add_node_group(cn: CompiledNode, page_id: Optional[str]) -> None:
        bc = cn.backend_call
        # A node branches if its @backend call is a branch, or if it
        # ends in an AirflowHitlBranch operator.
        node_branches = (bc is not None and bc.fn.is_branch) or any(
            t.kind == "airflow_hitl_branch" for t in cn.external_tasks
        )
        groups.append(
            GraphGroup(
                id=cn.id,
                title=cn.title,
                page_id=page_id,
                is_landing=cn.is_landing,
                is_branch=node_branches,
            )
        )
        input_ids = {f.name for f in cn.fields}
        node_input_ids[cn.id] = input_ids
        node_backend_fn[cn.id] = bc.fn.name if bc is not None else None

        # In-group rank — an input gated by a `When` on another input
        # of *this* node sits downstream of its controller. Build the
        # same-node condition graph and rank inputs by longest path.
        controllers: dict[str, set[str]] = {}
        for f in cn.fields:
            ctrls: set[str] = set()
            for cond in f.conditions:
                cnode = cond.get("node")
                cfield = cond.get("field")
                if (cnode is None or cnode == cn.id) and cfield in input_ids:
                    ctrls.add(cfield)
            controllers[f.name] = ctrls

        rank_of: dict[str, int] = {}

        def field_rank(name: str, stack: frozenset[str] = frozenset()) -> int:
            if name in rank_of:
                return rank_of[name]
            if name in stack:  # defensive — conditions are acyclic
                return 0
            ctrls = controllers.get(name, set())
            r = (
                0
                if not ctrls
                else 1
                + max(field_rank(c, stack | {name}) for c in ctrls)
            )
            rank_of[name] = r
            return r

        for f in cn.fields:
            field_rank(f.name)
        max_input_rank = max(rank_of.values(), default=-1)

        # Input nodes — ranked by the condition graph.
        for f in cn.fields:
            nodes.append(
                GraphNode(
                    id=f"{cn.id}::{f.name}",
                    kind="input",
                    group_id=cn.id,
                    label=f.label,
                    detail=f.type,
                    rank=rank_of[f.name],
                    required=f.required,
                )
            )

        # The node-internal @backend — downstream of every input.
        bk_id: Optional[str] = None
        if bc is not None:
            bk_id = backend_node_id(cn.id)
            nodes.append(
                GraphNode(
                    id=bk_id,
                    kind="backend",
                    group_id=cn.id,
                    label=bc.fn.name,
                    detail="branch" if bc.fn.is_branch else None,
                    rank=max_input_rank + 1,
                    is_branch=bc.fn.is_branch,
                )
            )
            # In-group edges: each input argument → the backend.
            # (Button arguments aren't graph nodes — skipped.)
            for arg in bc.arg_op_ids:
                if arg in input_ids:
                    edges.append(
                        GraphEdge(
                            id=f"ig:{cn.id}:{arg}->backend",
                            from_node=f"{cn.id}::{arg}",
                            to_node=bk_id,
                            relation="in_group",
                        )
                    )

        # Graph-visible external tasks — the Airflow operators — extend
        # the in-group sub-DAG past the backend: inputs → @backend →
        # trigger → sensor → … Plumbing tasks (graph_visible False, e.g.
        # the legacy AirflowStatus) stay hidden.
        ext_base_rank = (
            max_input_rank + 2 if bc is not None else max_input_rank + 1
        )
        prev_id = bk_id
        for i, ext in enumerate(
            t for t in cn.external_tasks if t.graph_visible
        ):
            ext_node_id = f"{cn.id}::{ext.task_id}"
            nodes.append(
                GraphNode(
                    id=ext_node_id,
                    kind="airflow",
                    group_id=cn.id,
                    label=ext.task_id,
                    detail=ext.kind,
                    rank=ext_base_rank + i,
                )
            )
            if prev_id is not None:
                edges.append(
                    GraphEdge(
                        id=f"ig:{cn.id}:{prev_id}->{ext_node_id}",
                        from_node=prev_id,
                        to_node=ext_node_id,
                        relation="in_group",
                    )
                )
            prev_id = ext_node_id

    # First pass — groups, their members, and the top-level order.
    order: list[str] = []  # top-level step ids in execution order
    for step in form.steps:
        if isinstance(step, CompiledPage):
            for sn in step.nodes:
                add_node_group(sn, step.id)
                order.append(sn.id)
        elif isinstance(step, CompiledBackendStep):
            nodes.append(
                GraphNode(
                    id=step.id,
                    kind="workflow_backend",
                    group_id=None,
                    label=step.id,
                    detail="branch" if step.is_branch else None,
                    rank=0,
                    is_branch=step.is_branch,
                )
            )
            order.append(step.id)
        else:  # CompiledNode — a top-level single-screen node
            add_node_group(step, None)
            order.append(step.id)

    # Execution edges — the `>>` flow, resolved to top-level granularity.
    seen: set[tuple[str, str]] = set()
    for step in form.steps:
        if isinstance(step, CompiledPage):
            for sn in step.nodes:
                for d in sn.downstream:
                    e = _entry_step_id(form, d)
                    if e and (sn.id, e) not in seen:
                        seen.add((sn.id, e))
                        edges.append(
                            GraphEdge(
                                id=f"ex:{sn.id}->{e}",
                                from_node=sn.id,
                                to_node=e,
                                relation="execution",
                            )
                        )
            for term in step.terminal_node_ids:
                for d in step.downstream:
                    e = _entry_step_id(form, d)
                    if e and (term, e) not in seen:
                        seen.add((term, e))
                        edges.append(
                            GraphEdge(
                                id=f"ex:{term}->{e}",
                                from_node=term,
                                to_node=e,
                                relation="execution",
                            )
                        )
        else:
            for src in _exit_step_ids(form, step.id):
                for d in step.downstream:
                    e = _entry_step_id(form, d)
                    if e and (src, e) not in seen:
                        seen.add((src, e))
                        edges.append(
                            GraphEdge(
                                id=f"ex:{src}->{e}",
                                from_node=src,
                                to_node=e,
                                relation="execution",
                            )
                        )

    # Dependency endpoints — a `steps.<node>.<field>` reference.
    node_exists = set(node_input_ids) | {
        n.id for n in nodes if n.kind == "workflow_backend"
    }

    def resolve_endpoint(node_id: str, field: Optional[str]) -> Optional[str]:
        # A whole-node reference resolves to the node's backend (what it
        # produces) — or to the group when it has no backend.
        if field is None:
            if node_backend_fn.get(node_id):
                return backend_node_id(node_id)
            return node_id if node_id in node_input_ids else None
        if field in node_input_ids.get(node_id, set()):
            return f"{node_id}::{field}"
        if node_backend_fn.get(node_id) == field:
            return backend_node_id(node_id)
        return None

    # Dependency edges from each node's static `steps` references —
    # options / default. Conditions are handled separately below, at
    # the input level (which inputs a `When` actually gates).
    for node in form.all_nodes_by_id.values():
        for dep in node.deps:
            if dep.source in ("template", "condition"):
                continue
            source = resolve_endpoint(dep.node, dep.field)
            target = (
                f"{node.id}::{dep.local}"
                if dep.local
                else node.id
            )
            if source is None:
                continue
            edges.append(
                GraphEdge(
                    id=f"dep:{source}->{target}:{dep.source}",
                    from_node=source,
                    to_node=target,
                    relation="dependency",
                    dep_source=dep.source,
                    dep_kind=dep.kind,
                )
            )

    # Condition edges — targeted at the inputs a `When` block gates.
    # Each input's cumulative visibility conditions name the controlling
    # input(s); one edge per controlling input → this gated input.
    for node in form.all_nodes_by_id.values():
        for f in node.fields:
            controllers: set[str] = set()
            for cond in f.conditions:
                cnode = cond.get("node") or node.id
                cfield = cond.get("field")
                if cfield and cfield in node_input_ids.get(cnode, set()):
                    controllers.add(f"{cnode}::{cfield}")
            target = f"{node.id}::{f.name}"
            for source in sorted(controllers):
                edges.append(
                    GraphEdge(
                        id=f"cond:{source}->{target}",
                        from_node=source,
                        to_node=target,
                        relation="dependency",
                        dep_source="condition",
                        dep_kind="functional",
                    )
                )

    # Dependency edges into workflow-level backend steps (their args).
    for step in form.steps:
        if not isinstance(step, CompiledBackendStep):
            continue
        for ref in list(step.arg_refs) + list(step.kwarg_refs.values()):
            source = resolve_endpoint(ref.get("node"), ref.get("name"))
            if source is None or step.id not in node_exists:
                continue
            edges.append(
                GraphEdge(
                    id=f"dep:{source}->{step.id}:argument",
                    from_node=source,
                    to_node=step.id,
                    relation="dependency",
                    dep_source="argument",
                    dep_kind="functional",
                )
            )

    return WorkflowGraph(
        form_id=form.id,
        title=form.title,
        groups=groups,
        nodes=nodes,
        edges=edges,
    )


@api.get("/forms/{form_id}/graph", response_model=WorkflowGraph, dependencies=[Depends(require_form_visibility)])
def read_form_graph(form_id: str) -> WorkflowGraph:
    """The form's compiled structural graph — inputs, node-internal
    backends, external tasks, and workflow-level backend steps as
    nodes; node-groups as containers; in-group, execution, and
    dependency edges. Rendered as a static DAG on the form summary."""
    form = _get_form_or_404(form_id)
    return _build_workflow_graph(form)

def _find_file_field(
    form: CompiledWorkflow, field_id: str
) -> tuple[dict[str, Any], str]:
    """Locate a file-upload field's spec across every node of a form.
    Returns `(file_spec, node_id)` — the node id is needed to slot
    same-screen draft values when resolving an S3 key template.
    Raises HTTPException(404) if no such file field exists."""
    for node in form.all_nodes_by_id.values():
        for f in node.fields:
            if f.name == field_id and f.file_spec is not None:
                return f.file_spec, node.id
    raise HTTPException(
        status_code=404,
        detail=f"no file field {field_id!r} in this form",
    )


class UploadResult(BaseModel):
    """The reference returned for an uploaded file — the form field's
    submitted value. A `file` upload carries an upload token; an
    `s3file` upload carries the S3 bucket and key."""
    kind: str  # "file" | "s3file"
    filename: str
    size: int
    content_type: str
    token: str | None = None  # file (transient)
    bucket: str | None = None  # s3file
    key: str | None = None  # s3file


@api.post(
    "/forms/{form_id}/uploads",
    response_model=UploadResult,
    dependencies=[Depends(require_form_visibility)],
)
async def upload_file(
    form_id: str,
    field_id: str = Form(...),
    file: UploadFile = Form(...),
    submission_id: str | None = Form(None),
    draft_values: str | None = Form(None),
) -> UploadResult:
    """Receive a file for a `File` / `S3File` field.

    The bytes stream through the backend. A `file` field's bytes are
    held as an upload blob (returned as a token); an `s3file` field's
    bytes are streamed to S3 (returned as a bucket/key reference). The
    form submission then carries whichever reference back. Size and
    extension limits are enforced here, server-side.

    For `s3file`, the field's `key` template is resolved here. Tokens
    naming an earlier step resolve against the submission's upstream
    data (`submission_id`); tokens naming the upload's own screen
    resolve against `draft_values` — a JSON snapshot of that screen's
    current field values, sent by the browser.
    """
    form = _get_form_or_404(form_id)
    spec, node_id = _find_file_field(form, field_id)

    data = await file.read()
    filename = file.filename or "upload"
    content_type = file.content_type or "application/octet-stream"

    # Size limit.
    max_bytes = int(spec["max_size_mb"] * 1024 * 1024)
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                f"file exceeds the {spec['max_size_mb']} MB limit "
                f"for this field"
            ),
        )
    # Extension allow-list.
    accept = spec.get("accept") or []
    if accept:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in accept:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"file type .{ext or '?'} is not accepted — "
                    f"allowed: {', '.join('.' + a for a in accept)}"
                ),
            )

    if spec["kind"] == "s3file":
        # Resolve the key template. Build the `steps` lookup from the
        # submission's upstream data, then overlay this screen's draft
        # values under its own node id so same-screen tokens resolve.
        steps_data: dict[str, dict[str, Any]] = {}
        if submission_id:
            try:
                _, submission = _get_submission_or_404(
                    form_id, submission_id
                )
                steps_data = build_steps_with_workflow(form, submission)
            except HTTPException:
                # An unknown submission id just means no upstream data;
                # the template falls back to draft values / literals.
                steps_data = {}
        if draft_values:
            try:
                draft = json.loads(draft_values)
                if isinstance(draft, dict):
                    merged = dict(steps_data.get(node_id, {}))
                    merged.update(draft)
                    steps_data[node_id] = merged
            except (ValueError, TypeError):
                pass  # malformed draft — ignore, resolve without it

        resolved_key = uploads.resolve_s3_key(
            spec.get("key", ""),
            filename=filename,
            steps=steps_data,
        )
        if not resolved_key:
            raise HTTPException(
                status_code=422,
                detail=(
                    "the S3File key template resolved to an empty "
                    "key — check its `steps` references"
                ),
            )

        try:
            ref = uploads.put_s3_object(
                data=data,
                key=resolved_key,
                filename=filename,
                content_type=content_type,
                bucket=spec.get("bucket"),
            )
        except uploads.UploadError as e:
            # S3 misconfigured or unreachable — fail loudly.
            raise HTTPException(status_code=502, detail=str(e)) from e
        return UploadResult(
            kind="s3file",
            filename=ref["filename"],
            size=ref["size"],
            content_type=ref["content_type"],
            bucket=ref["bucket"],
            key=ref["key"],
        )

    # file (transient) — hold the bytes as an upload blob.
    token = secrets.token_urlsafe(24)
    store.create_upload_blob(
        token=token,
        form_id=form_id,
        field_id=field_id,
        filename=filename,
        content_type=content_type,
        data=data,
    )
    return UploadResult(
        kind="file",
        filename=filename,
        size=len(data),
        content_type=content_type,
        token=token,
    )


@api.post(
    "/forms/{form_id}/submissions",
    response_model=SubmissionResponse,
    status_code=201,
    dependencies=[Depends(require_form_visibility)],
)
def create_submission(
    form_id: str, req: StartSubmissionRequest
) -> SubmissionResponse:
    form = _get_form_or_404(form_id)
    _validate_landing_values(form, req.values)
    # Drop values for fields hidden by an unsatisfied `When`. The
    # landing node has no upstream, so its conditions are all same-node.
    values = _prune_inactive(
        form.landing_node().fields, dict(req.values), {}
    )

    try:
        submission = start_submission(
            form,
            initial_form_values=values,
            form_version_id=FORM_VERSION_IDS.get(form_id, 0),
        )
    except ValueError as e:
        # Submission-id template misconfiguration or collision.
        raise HTTPException(status_code=409, detail=str(e)) from e

    advance(form, submission)
    _persist(form, submission)

    # Tie any transient-file upload blobs to the submission, so they
    # can be found for cleanup when it ends. Tokens come from the
    # file-field values in the landing step.
    _attach_upload_blobs(form, values, submission.handle)

    return _build_submission_response(submission, form)


def _attach_upload_blobs(
    form: CompiledWorkflow,
    values: dict[str, Any],
    submission_handle: str,
) -> None:
    """Find `file`-field upload tokens in a set of submitted values and
    tie their blobs to the submission."""
    file_fields = {
        f.name
        for node in form.all_nodes_by_id.values()
        for f in node.fields
        if f.type == "file"
    }
    tokens = [
        v["token"]
        for k, v in values.items()
        if k in file_fields
        and isinstance(v, dict)
        and v.get("token")
    ]
    if tokens:
        store.attach_upload_blobs(tokens, submission_handle)


@api.get(
    "/forms/{form_id}/submissions/{submission_id}",
    response_model=SubmissionResponse,
    dependencies=[Depends(require_form_visibility)],
)
def read_submission(form_id: str, submission_id: str) -> SubmissionResponse:
    form, submission = _get_submission_or_404(form_id, submission_id)
    advance(form, submission)
    _persist(form, submission)
    return _build_submission_response(submission, form)


@api.get(
    "/forms/{form_id}/submissions/{submission_id}/steps/{step_id}",
    response_model=StepDetail,
    dependencies=[Depends(require_form_visibility)],
)
def read_step(form_id: str, submission_id: str, step_id: str) -> StepDetail:
    form, submission = _get_submission_or_404(form_id, submission_id)
    advance(form, submission)
    _persist(form, submission)

    ng = form.all_nodes_by_id.get(step_id)
    if ng is None:
        if isinstance(form.by_id.get(step_id), CompiledBackendStep):
            raise HTTPException(
                status_code=404,
                detail=f"step {step_id!r} is a backend step, not a form",
            )
        raise HTTPException(status_code=404, detail="step not found")

    step = next(
        (s for s in submission.steps if s.node_id == step_id), None
    )
    if step is None:
        raise HTTPException(
            status_code=404,
            detail=f"step {step_id!r} has not been reached in this submission",
        )

    response_payload: Optional[dict[str, Any]] = None
    if step.is_submitted:
        response_payload = {
            "values": dict(step.form_values or {}),
            "button": step.button_clicked,
        }

    # An open step that still holds values was re-opened — surface them
    # (with the prior button) so the form pre-fills.
    draft_payload: Optional[dict[str, Any]] = None
    if not step.is_submitted and step.form_values:
        draft_payload = {
            "values": dict(step.form_values),
            "button": step.button_clicked,
        }

    page = form.node_page.get(step_id)
    return StepDetail(
        handle=submission.handle,
        submission_id=submission.submission_id,
        step_id=step_id,
        layout=_to_block(
            resolve_layout(form, submission, ng.layout, step_id)
        ),
        response_received=step.is_submitted,
        response=response_payload,
        draft=draft_payload,
        status=step.status.key,
        edit_in_progress=submission.editing_node_id == step_id,
        responded_at=(
            step.submitted_at.isoformat() if step.submitted_at else None
        ),
        is_landing=ng.is_landing,
        page_id=page.id if page is not None else None,
        page_title=page.title if page is not None else None,
    )


@api.post(
    "/forms/{form_id}/submissions/{submission_id}/steps/{step_id}",
    response_model=StepDetail,
    dependencies=[Depends(require_form_visibility)],
)
def submit_step_endpoint(
    form_id: str,
    submission_id: str,
    step_id: str,
    req: StepSubmissionRequest,
) -> StepDetail:
    form, submission = _get_submission_or_404(form_id, submission_id)
    advance(form, submission)

    ng = form.all_nodes_by_id.get(step_id)
    if ng is None:
        if isinstance(form.by_id.get(step_id), CompiledBackendStep):
            raise HTTPException(
                status_code=404,
                detail=f"step {step_id!r} is a backend step — nothing "
                "to submit",
            )
        raise HTTPException(status_code=404, detail="step not found")

    # `button` is the clicked button's id. Optional for single-button
    # steps — the runtime falls back to the only button.
    button_clicked = req.button
    if button_clicked is not None:
        valid_ids = {b.id for b in ng.buttons}
        if button_clicked not in valid_ids:
            raise HTTPException(
                status_code=400,
                detail=f"unknown button {button_clicked!r}; "
                f"expected one of {sorted(valid_ids)}",
            )
    elif len(ng.buttons) > 1:
        raise HTTPException(
            status_code=400,
            detail="this step has multiple buttons; `button` is required",
        )

    try:
        # Prune fields hidden by an unsatisfied `When`. Cross-node
        # conditions are evaluated against the submission's upstream
        # data, same-node ones against the values just submitted.
        steps_data = build_steps_with_workflow(form, submission)
        # Required-field check for this step — generic over any node.
        _validate_required_fields(ng, dict(req.values), steps_data)
        pruned = _prune_inactive(
            ng.fields, dict(req.values), steps_data
        )
        submit_step(
            form,
            submission,
            node_id=step_id,
            form_values=pruned,
            button_clicked=button_clicked,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # Tie any file uploads submitted on this step to the submission.
    _attach_upload_blobs(form, pruned, submission.handle)

    return read_step(form_id, submission_id, step_id)


class HitlResponseRequest(BaseModel):
    """A user's answer to an Airflow HITL prompt."""
    chosen_options: list[str]
    # Values for the action's param schema, when it has one.
    params_input: dict[str, Any] = {}


@api.post(
    "/forms/{form_id}/submissions/{submission_id}/hitl/{task_id}",
    response_model=StepDetail,
    dependencies=[Depends(require_form_visibility)],
)
def respond_hitl_endpoint(
    form_id: str,
    submission_id: str,
    task_id: str,
    req: HitlResponseRequest,
) -> StepDetail:
    """Submit a user's response to an Airflow HITL task, resuming the
    paused DAG. `task_id` is the HITL operator's id.

    A delivery failure returns 502 and leaves the submission untouched,
    so the user can retry — a HITL hiccup must not fail the form."""
    form, submission = _get_submission_or_404(form_id, submission_id)
    advance(form, submission)

    # Locate the HITL operator and the step it trails, and confirm it is
    # actually awaiting a response right now.
    located: Optional[tuple[Any, Any]] = None
    owner_node_id: Optional[str] = None
    for step in submission.steps:
        node = form.all_nodes_by_id.get(step.node_id)
        if node is None:
            continue
        for ext in node.external_tasks:
            if ext.task_id == task_id and ext.kind in (
                "airflow_hitl", "airflow_hitl_branch"
            ):
                located = (step, ext)
                owner_node_id = step.node_id
                break
        if located is not None:
            break

    if located is None:
        raise HTTPException(
            status_code=404, detail=f"HITL task {task_id!r} not found"
        )
    step, ext = located
    current = step.external_state.get(task_id) or {}
    if current.get("state") != "awaiting_response":
        raise HTTPException(
            status_code=409,
            detail=f"HITL task {task_id!r} is not awaiting a response",
        )

    steps_data = build_steps_with_workflow(form, submission)

    def resolve(template_str: str) -> Any:
        return render(template_str, steps_data)

    try:
        new_state = respond_to_hitl(
            ext,
            resolve=resolve,
            get_hook=_airflow_hook_for,
            chosen_options=req.chosen_options,
            params_input=req.params_input,
        )
    except AirflowError as e:
        raise HTTPException(
            status_code=502, detail=f"could not deliver HITL response: {e}"
        ) from e

    # Record the response and let the chain advance past the HITL step.
    step.external_state[task_id] = new_state
    advance(form, submission)
    return read_step(form_id, submission_id, owner_node_id)


@api.post(
    "/forms/{form_id}/submissions/{submission_id}/clear",
    response_model=ClearResponse,
    dependencies=[Depends(require_form_visibility)],
)
def clear_submission(
    form_id: str, submission_id: str, req: ClearRequest
) -> ClearResponse:
    form, submission = _get_submission_or_404(form_id, submission_id)
    advance(form, submission)

    # A full reset (no from_task_id) restarts the submission from the
    # landing step — semantically "run this over". It should run over on
    # the *current* live form version, not the version it first ran on:
    # this is how a failed run is retried after its workflow file is
    # fixed and reloaded with POST /refresh. A partial clear keeps the
    # pinned version — it's an in-flight edit of the run as it stands.
    if (
        req.from_task_id is None
        and not req.dry_run
        and form_id in FORMS
    ):
        live_version = FORM_VERSION_IDS.get(form_id)
        if (
            live_version is not None
            and live_version != submission.form_version_id
        ):
            form = FORMS[form_id]
            submission.form_version_id = live_version

    # The landing step sets the submission id. Rewinding it individually
    # would re-open its form and let the user submit different values,
    # invalidating the id in the URL. It can only be reset as part of a
    # full-submission clear (from_task_id omitted), which replays the
    # landing step with its original, frozen values.
    if (
        req.from_task_id is not None
        and req.from_task_id == form.landing_node().id
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"step {req.from_task_id!r} is the landing step and cannot "
                "be cleared individually — it sets the submission id. "
                "Reset the whole submission instead (omit from_task_id)."
            ),
        )

    # "edit" re-opens one step with its answers — it has no meaning for
    # a full-submission rewind.
    if req.mode == "edit" and req.from_task_id is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "edit mode applies to a single step — supply from_task_id, "
                "or use a full reset (mode=reset) to rewind the whole run."
            ),
        )

    if req.dry_run:
        if req.from_task_id is None:
            affected = [s.node_id for s in submission.steps]
        else:
            idx = next(
                (
                    i
                    for i, s in enumerate(submission.steps)
                    if s.node_id == req.from_task_id
                ),
                None,
            )
            affected = (
                [s.node_id for s in submission.steps[idx:]]
                if idx is not None
                else []
            )
        return ClearResponse(affected_tasks=affected, cleared=False)

    affected = clear_submission_from(
        form, submission, req.from_task_id, req.mode, req.scope
    )
    advance(form, submission)
    _persist(form, submission)
    return ClearResponse(affected_tasks=affected, cleared=True)


# --- Forms & submissions: listing + tracking -------------------------------
#
# These read straight from the data backend, not the in-memory runtime,
# so they cover every persisted submission — including those of archived
# forms whose DSL file is no longer present. `GET /forms` is the forms
# index; `GET /forms/{id}/submissions` lists a form's submissions (the
# same path POSTed to to create one).


class SubmissionCounts(BaseModel):
    running: int
    success: int
    failed: int
    total: int


class FormSummary(BaseModel):
    form_id: str
    name: str
    # Relative folder of the form's file; "" for the top level. Folder
    # grouping is left to the client.
    folder_path: str
    # Whether the form's DSL file is still present in the latest scan.
    is_live: bool
    version_count: int
    submissions: SubmissionCounts
    last_activity: Optional[datetime] = None


class SubmissionSummary(BaseModel):
    # Null only for a draft — but drafts are never persisted, so in
    # practice every listed submission has an id.
    submission_id: Optional[str] = None
    handle: str
    state: str
    # The form version (the human-facing integer) this submission ran on.
    form_version: int
    created_at: datetime
    terminated_at: Optional[datetime] = None
    # Node id of the submission's current (latest) step.
    current_step: Optional[str] = None


@api.get("/forms", response_model=list[FormSummary])
def list_forms(
    user: "store.User" = Depends(_current_user),
) -> list[FormSummary]:
    """The forms index — every form the signed-in user may see, with
    its folder, live status, version count, and submission counts by
    state. Ordered by folder then form id; the client groups by
    `folder_path`.

    An admin sees every form; a non-admin sees only forms in folders a
    group grant covers."""
    rows = store.list_forms_overview()
    granted = auth.accessible_form_folders(user)
    if granted is not None:
        rows = [
            r
            for r in rows
            if auth.folder_is_accessible(granted, r["folder_path"])
        ]
    return [FormSummary(**row) for row in rows]


@api.get(
    "/forms/{form_id}/submissions",
    response_model=list[SubmissionSummary],
    dependencies=[Depends(require_form_access("view"))],
)
def read_form_submissions(form_id: str) -> list[SubmissionSummary]:
    """Every submission of a form, newest first — state, timing, the
    version it ran on, and its current step."""
    if not store.form_exists(form_id):
        raise HTTPException(
            status_code=404, detail=f"form {form_id!r} not found"
        )
    return [
        SubmissionSummary(**row)
        for row in store.list_form_submissions(form_id)
    ]


class StepDetailRow(BaseModel):
    seq: int
    node_id: str
    # Node display title — null for a workflow-level backend step.
    title: Optional[str] = None
    page_id: Optional[str] = None
    kind: str  # node | backend
    state: str  # awaiting | submitted | failed
    # Edit-cascade status — "unaffected" | "needs_review" | "needs_input".
    status: str = "unaffected"
    started_at: Optional[datetime] = None
    submitted_at: Optional[datetime] = None
    form_values: Optional[dict[str, Any]] = None
    backend_return: Any = None
    button_clicked: Optional[str] = None


class EventRow(BaseModel):
    type: str
    node_id: Optional[str] = None
    page_id: Optional[str] = None
    occurred_at: datetime
    payload: Optional[dict[str, Any]] = None


class SubmissionDetail(BaseModel):
    submission_id: Optional[str] = None
    handle: str
    form_id: str
    state: str
    # The form version (human-facing integer) this submission ran on.
    form_version: int
    created_at: datetime
    terminated_at: Optional[datetime] = None
    error: Optional[str] = None
    steps: list[StepDetailRow]
    events: list[EventRow]


@api.get(
    "/forms/{form_id}/submissions/{submission_id}/detail",
    response_model=SubmissionDetail,
)
def read_submission_detail(
    form_id: str, submission_id: str
) -> SubmissionDetail:
    """The persisted record of one submission — every step with the
    data it captured, plus the append-only event history. Powers the
    submission summary page; distinct from the runtime DAG view at
    `GET /forms/{id}/submissions/{id}`."""
    form, submission = _get_submission_or_404(form_id, submission_id)
    advance(form, submission)
    _persist(form, submission)

    snap = submission_snapshot(form, submission)
    fv = store.get_form_version(snap["form_version_id"])
    version = fv["version"] if fv is not None else 0

    steps: list[StepDetailRow] = []
    for s in snap["steps"]:
        node = form.all_nodes_by_id.get(s["node_id"])
        steps.append(
            StepDetailRow(
                seq=s["seq"],
                node_id=s["node_id"],
                title=node.title if node is not None else None,
                page_id=s["page_id"],
                kind=s["kind"],
                state=s["state"],
                status=s.get("status") or "unaffected",
                started_at=s["started_at"],
                submitted_at=s["submitted_at"],
                form_values=s["form_values"],
                backend_return=s["backend_return"],
                button_clicked=s["button_clicked"],
            )
        )

    return SubmissionDetail(
        submission_id=snap["submission_id"],
        handle=snap["handle"],
        form_id=form.id,
        state=snap["state"],
        form_version=version,
        created_at=snap["created_at"],
        terminated_at=snap["terminated_at"],
        error=snap["error"],
        steps=steps,
        events=[EventRow(**e) for e in snap["events"]],
    )


@api.get("/health")
def health() -> dict[str, Any]:
    """Liveness probe + a snapshot of what's loaded. `load_errors` lists
    any form file that failed to import or compile in the last scan."""
    return {
        "app": "frontflow",
        "forms": sorted(FORMS.keys()),
        "load_errors": LOAD_ERRORS,
    }


@api.post("/refresh", dependencies=[Depends(require_admin)])
def refresh() -> dict[str, Any]:
    """Re-scan WORKFLOWS_DIR and rebuild the form registry without a
    restart. Intended to be called by CI/CD after new form files are
    deployed. Returns the forms now being served and any load errors."""
    scan_workflows()
    return {
        "forms": sorted(FORMS.keys()),
        "load_errors": LOAD_ERRORS,
    }


@api.post("/forms/{form_id}/refresh", dependencies=[Depends(require_form_access("manage"))])
def refresh_form(form_id: str) -> dict[str, Any]:
    """Reparse the workflow source and report the result for one form —
    for a per-form 'reparse' action, or a CI/CD hook scoped to a single
    form. The rescan itself is whole-source (idempotent and fast); the
    response is narrowed to the named form.

    Returns the form's live state and, if it failed to load or compile,
    the error — so a caller sees immediately whether a fix took."""
    scan_workflows()
    if form_id in FORMS:
        return {
            "form_id": form_id,
            "status": "live",
            "version_id": FORM_VERSION_IDS.get(form_id),
        }
    error = LOAD_ERRORS.get(form_id)
    if error is not None:
        return {"form_id": form_id, "status": "error", "error": error}
    raise HTTPException(
        status_code=404,
        detail=f"no workflow named {form_id!r} in the workflow source",
    )


# --- Submission export API -------------------------------------------------
#
# A pull-based batch API for collecting submission data into an external
# system. Bearer-token protected (FRONTFLOW_API_TOKEN). Submissions of
# every state are returned — in-flight as well as terminated.

_EXPORT_MAX_LIMIT = 500
_EXPORT_DEFAULT_LIMIT = 100


def _parse_iso_param(value: str) -> Optional[datetime]:
    """Parse an ISO 8601 datetime from a query parameter.

    A correctly URL-encoded value parses directly. As a fallback, a
    value whose "+HH:MM" timezone offset was decoded to " HH:MM" (an
    unencoded '+' in a query string becomes a space) is repaired —
    only the trailing offset, so a date/time-separating space is not
    disturbed. Returns None if it cannot be parsed.
    """
    value = value.strip()
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    # Fallback: a trailing " HH:MM" that was meant to be "+HH:MM".
    repaired = re.sub(r" (\d{2}:\d{2})$", r"+\1", value)
    if repaired != value:
        try:
            return datetime.fromisoformat(repaired)
        except ValueError:
            pass
    return None


@api.get("/export/submissions", dependencies=[Depends(require_api_token)])
def export_submissions(
    cursor: str | None = None,
    updated_since: str | None = None,
    updated_before: str | None = None,
    form_id: str | None = None,
    terminal_only: bool = False,
    limit: int = _EXPORT_DEFAULT_LIMIT,
) -> dict[str, Any]:
    """A page of submission records for batch collection.

    Walk the whole set by passing the returned `next_cursor` back each
    call until it is null.

    `updated_since` and `updated_before` (both ISO 8601) bound the
    submission's last-update time as a half-open interval
    [since, before) — a fixed window always returns the same rows, so
    a windowed pull is safely retryable. Either bound may be given
    alone; `updated_since` alone is an open-ended "catch me up to now".

    The feed is at-least-once: a submission's update time moves on
    every state change, so an in-flight submission can appear in
    several successive windows. Consumers upsert on `submission_id`.
    Pass `terminal_only=true` to restrict to terminated submissions,
    whose update time is frozen — each then falls in a single window
    (unless an operator later retries it).

    `form_id` restricts to one form. Records cover every submission
    state by default, in-flight included; each carries the submitted
    form values in a per-step breakdown.
    """
    if limit < 1 or limit > _EXPORT_MAX_LIMIT:
        raise HTTPException(
            status_code=422,
            detail=f"limit must be between 1 and {_EXPORT_MAX_LIMIT}",
        )

    since_dt: Optional[datetime] = None
    if updated_since is not None:
        since_dt = _parse_iso_param(updated_since)
        if since_dt is None:
            raise HTTPException(
                status_code=422,
                detail="updated_since must be an ISO 8601 datetime",
            )

    before_dt: Optional[datetime] = None
    if updated_before is not None:
        before_dt = _parse_iso_param(updated_before)
        if before_dt is None:
            raise HTTPException(
                status_code=422,
                detail="updated_before must be an ISO 8601 datetime",
            )

    if (
        since_dt is not None
        and before_dt is not None
        and since_dt >= before_dt
    ):
        raise HTTPException(
            status_code=422,
            detail="updated_since must be earlier than updated_before",
        )

    try:
        return store.export_submissions(
            limit=limit,
            cursor=cursor,
            updated_since=since_dt,
            updated_before=before_dt,
            form_id=form_id,
            terminal_only=terminal_only,
        )
    except ValueError as e:
        # A malformed cursor, or one that doesn't match this query.
        raise HTTPException(status_code=422, detail=str(e))


# --- Authentication --------------------------------------------------------
#
# Hosted accounts for the admin console. Login issues a server-side
# session carried in an HttpOnly cookie; the require_admin dependency
# gates the console surface against it.


class LoginRequest(BaseModel):
    username: str
    password: str


class UserInfo(BaseModel):
    username: str
    is_admin: bool
    must_change_password: bool = False


@api.post("/auth/login", response_model=UserInfo)
def login(req: LoginRequest, response: Response) -> UserInfo:
    """Authenticate and open a session. Sets the session cookie."""
    user = auth.authenticate(req.username, req.password)
    if user is None:
        raise HTTPException(
            status_code=401, detail="invalid username or password"
        )
    token = auth.create_session(user.id)
    # HttpOnly: not readable by JS. SameSite=Lax: sent on top-level
    # navigations but not cross-site POSTs — basic CSRF protection.
    response.set_cookie(
        key=_SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=int(auth.SESSION_LIFETIME.total_seconds()),
    )
    return UserInfo(
        username=user.username,
        is_admin=user.is_admin,
        must_change_password=user.must_change_password,
    )


@api.post("/auth/logout")
def logout(
    response: Response,
    frontflow_session: str | None = Cookie(default=None),
) -> dict[str, bool]:
    """End the current session and clear the cookie."""
    auth.revoke_session(frontflow_session)
    response.delete_cookie(key=_SESSION_COOKIE)
    return {"ok": True}


@api.get("/auth/me", response_model=UserInfo)
def whoami(
    frontflow_session: str | None = Cookie(default=None),
) -> UserInfo:
    """The currently authenticated user, or 401."""
    user = auth.resolve_session(frontflow_session)
    if user is None:
        raise HTTPException(
            status_code=401, detail="not authenticated"
        )
    return UserInfo(
        username=user.username,
        is_admin=user.is_admin,
        must_change_password=user.must_change_password,
    )


# --- Access management (admin-only) ----------------------------------------
#
# Groups, membership, and folder grants — the M2 authorization model.
# All of it is admin-only; a folder grant cascades to every form under
# its path, conferring 'view' or 'manage' on the groups that hold it.


class GroupCreate(BaseModel):
    name: str


class GrantCreate(BaseModel):
    folder_path: str = ""
    role: str  # 'view' | 'manage'


class MemberChange(BaseModel):
    user_id: int


@api.get("/users", dependencies=[Depends(require_admin)])
def list_users() -> list[dict[str, Any]]:
    """All user accounts — for assigning group membership and for the
    user-management console."""
    return auth.list_users()


class UserCreate(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class PasswordReset(BaseModel):
    new_password: str


class AdminToggle(BaseModel):
    is_admin: bool


class ActiveToggle(BaseModel):
    is_active: bool


class OwnPasswordChange(BaseModel):
    current_password: str
    new_password: str


@api.post(
    "/users", status_code=201, dependencies=[Depends(require_admin)]
)
def create_user_endpoint(req: UserCreate) -> dict[str, Any]:
    """Create a user account from the console."""
    try:
        user = auth.create_user(
            req.username, req.password, is_admin=req.is_admin
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {
        "id": user.id,
        "username": user.username,
        "is_admin": user.is_admin,
    }


@api.put(
    "/users/{user_id}/password",
    dependencies=[Depends(require_admin)],
)
def reset_user_password(
    user_id: int, req: PasswordReset
) -> dict[str, bool]:
    """Admin reset of a user's password. The user must change it at
    next login; their existing sessions are ended."""
    try:
        auth.set_user_password(user_id, req.new_password)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@api.put("/users/{user_id}/admin")
def set_user_admin_endpoint(
    user_id: int,
    req: AdminToggle,
    actor: "store.User" = Depends(require_admin),
) -> dict[str, bool]:
    """Promote or demote a user. An admin cannot demote themselves;
    the last admin cannot be demoted."""
    if actor.id == user_id and not req.is_admin:
        raise HTTPException(
            status_code=403,
            detail="you cannot remove your own admin access",
        )
    try:
        auth.set_user_admin(user_id, req.is_admin)
    except ValueError as e:
        # "user not found" vs. the last-admin guard.
        code = 404 if "not found" in str(e) else 409
        raise HTTPException(status_code=code, detail=str(e))
    return {"ok": True}


@api.put("/users/{user_id}/active")
def set_user_active_endpoint(
    user_id: int,
    req: ActiveToggle,
    actor: "store.User" = Depends(require_admin),
) -> dict[str, bool]:
    """Activate or deactivate an account. An admin cannot deactivate
    themselves; the last admin cannot be deactivated."""
    if actor.id == user_id and not req.is_active:
        raise HTTPException(
            status_code=403,
            detail="you cannot deactivate your own account",
        )
    try:
        auth.set_user_active(user_id, req.is_active)
    except ValueError as e:
        code = 404 if "not found" in str(e) else 409
        raise HTTPException(status_code=code, detail=str(e))
    return {"ok": True}


@api.delete("/users/{user_id}")
def delete_user_endpoint(
    user_id: int,
    actor: "store.User" = Depends(require_admin),
) -> dict[str, bool]:
    """Hard-delete an account. An admin cannot delete themselves; the
    last admin cannot be deleted."""
    if actor.id == user_id:
        raise HTTPException(
            status_code=403,
            detail="you cannot delete your own account",
        )
    try:
        auth.delete_user(user_id)
    except ValueError as e:
        code = 404 if "not found" in str(e) else 409
        raise HTTPException(status_code=code, detail=str(e))
    return {"ok": True}


@api.post("/auth/change-password")
def change_own_password_endpoint(
    req: OwnPasswordChange,
    actor: "store.User" = Depends(_current_user),
) -> dict[str, bool]:
    """A signed-in user changing their own password. Requires the
    current password; clears any forced-change flag."""
    try:
        auth.change_own_password(
            actor.id, req.current_password, req.new_password
        )
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True}


@api.get("/groups", dependencies=[Depends(require_admin)])
def list_groups() -> list[dict[str, Any]]:
    """All groups, with member and grant counts."""
    return auth.list_groups()


@api.post(
    "/groups", status_code=201, dependencies=[Depends(require_admin)]
)
def create_group(req: GroupCreate) -> dict[str, Any]:
    """Create a group."""
    try:
        return auth.create_group(req.name)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@api.get("/groups/{group_id}", dependencies=[Depends(require_admin)])
def get_group(group_id: int) -> dict[str, Any]:
    """A group with its members and folder grants."""
    try:
        return auth.group_detail(group_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@api.delete(
    "/groups/{group_id}", dependencies=[Depends(require_admin)]
)
def delete_group(group_id: int) -> dict[str, bool]:
    """Delete a group, its memberships, and its grants."""
    try:
        auth.delete_group(group_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@api.post(
    "/groups/{group_id}/members",
    dependencies=[Depends(require_admin)],
)
def add_group_member(
    group_id: int, req: MemberChange
) -> dict[str, bool]:
    """Add a user to a group."""
    try:
        auth.add_member(group_id, req.user_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@api.delete(
    "/groups/{group_id}/members/{user_id}",
    dependencies=[Depends(require_admin)],
)
def remove_group_member(
    group_id: int, user_id: int
) -> dict[str, bool]:
    """Remove a user from a group."""
    auth.remove_member(group_id, user_id)
    return {"ok": True}


@api.post(
    "/groups/{group_id}/grants",
    status_code=201,
    dependencies=[Depends(require_admin)],
)
def add_group_grant(
    group_id: int, req: GrantCreate
) -> dict[str, Any]:
    """Grant a group a role on a folder subtree."""
    try:
        return auth.add_grant(group_id, req.folder_path, req.role)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@api.delete(
    "/grants/{grant_id}", dependencies=[Depends(require_admin)]
)
def remove_grant(grant_id: int) -> dict[str, bool]:
    """Delete a folder grant."""
    auth.remove_grant(grant_id)
    return {"ok": True}


# --- Serve the bundled frontend --------------------------------------------
#
# Every data route is mounted under /api here — registered before the
# SPA catch-all below so API calls always win. The built React app
# ships inside the package at frontflow/static/; any non-/api, non-asset
# path returns index.html so the single-page app handles the route.

app.include_router(api, prefix="/api")

from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

_STATIC_DIR = Path(__file__).parent / "static"

if _STATIC_DIR.is_dir():
    # Hashed build assets live under /assets — mount them directly.
    _assets = _STATIC_DIR / "assets"
    if _assets.is_dir():
        app.mount(
            "/assets", StaticFiles(directory=_assets), name="assets"
        )

    @app.get("/{full_path:path}", include_in_schema=False)
    def _spa(full_path: str) -> FileResponse:
        """Serve a bundled static file, or index.html for any other
        path so the single-page app can handle the route itself."""
        # An unmatched /api path is a genuine 404 — never the SPA.
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="not found")
        candidate = (_STATIC_DIR / full_path).resolve()
        # Stay inside the static dir; serve the file if it exists.
        if (
            _STATIC_DIR in candidate.parents
            and candidate.is_file()
        ):
            return FileResponse(candidate)
        return FileResponse(_STATIC_DIR / "index.html")
