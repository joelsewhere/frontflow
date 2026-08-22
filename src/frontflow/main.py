"""
FastAPI app for the Workflow Runner backend.

Discovers form files from a configurable directory (WORKFLOWS_DIR),
executes and compiles each one in isolation, and serves them as forms
keyed by their `form_id`. A bad form file is skipped, not fatal;
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
from datetime import datetime, timedelta, timezone
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
    Query,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from frontflow.dsl import WORKFLOWS, WORKSPACES, store
from frontflow.dsl import auth
from frontflow.dsl import uploads
from frontflow.dsl.compile import (
    CompiledBackendStep,
    CompiledNode,
    CompiledPage,
    CompiledWorkflow,
    WorkflowSourceUnavailable,
    compile_workflow,
    compiled_graph_to_workflow,
    serialize_workflow,
    workflow_content_hash,
)
from frontflow.dsl.runtime import (
    NeedsPreviewBranchChoice,
    Submission,
    advance,
    build_steps_with_workflow,
    clear_submission_from,
    delete_preview_submission,
    get_preview_submission,
    get_submission,
    hydrate_submission,
    jump_preview,
    list_preview_submissions,
    resolve_layout,
    resolve_preview_branch,
    resolve_template,
    start_submission,
    submission_snapshot,
    submit_step,
    repin_submission,
    force_repin_submission,
    selective_force_repin_submission,
    validate_repin,
)
from frontflow.dsl.airflow_dispatch import respond_to_hitl
from frontflow.dsl.airflow_hook import AirflowError
from frontflow.dsl.runtime import _airflow_hook_for
from frontflow.dsl import runtime  # module-level import for hydrate_state's id-index priming
from frontflow.dsl.templating import render
from frontflow.dsl.sources import workflow_source_from_uri


# --- Workflow loading ------------------------------------------------------
#
# Form files are discovered from a directory — WORKFLOWS_DIR, configurable
# so the service can read from a deploy location that CI/CD pushes to.
# Each file is executed in isolation; running it registers whatever @form
# workflows it defines, the same way Airflow discovers DAGs from its dags
# folder. A form's id is the form_id from its @form decorator (the
# function name, or an explicit `form_id=`) — never the filename.
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


# Environment default for auto-repinning in-flight submissions onto a
# new minor version (a source-text change that didn't bump the
# compiled-graph structure). True in dev (so a code edit "just shows
# up" in running submissions); False in production (so old runs stay
# pinned to the version they started on, preserving audit fidelity).
# A form can override either way via `@form(auto_repin_minor=...)`;
# see `_should_auto_repin_minor` for the resolution order.
#
# Accepted true values: 1, true, yes, on (case-insensitive). Anything
# else, including the unset case, resolves to False.
def _read_auto_repin_env() -> bool:
    raw = os.environ.get("FRONTFLOW_AUTO_REPIN_MINOR", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


AUTO_REPIN_MINOR_ENV_DEFAULT = _read_auto_repin_env()


def _should_auto_repin_minor(wf: Any) -> bool:
    """Resolve whether minor bumps on `wf` should auto-repin in-flight
    submissions. Per-form DSL override beats the env var; absent any
    override, use the env default.

    Note that the env default is captured at module import time, not
    re-read on every scan. A change to FRONTFLOW_AUTO_REPIN_MINOR
    requires a restart — matching how every other frontflow env var
    is treated.
    """
    form_setting = getattr(wf, "auto_repin_minor", None)
    if form_setting is not None:
        return form_setting
    return AUTO_REPIN_MINOR_ENV_DEFAULT


# Install-wide allowlist for embedded /my-tasks panels (Phase 6).
# Comma-separated origins (or "*" for any). Empty / unset disables
# embedded /my-tasks entirely — the route returns 404 in that case,
# matching how restricted forms behave (existence not leaked by URL
# probing). Configure via env var, NOT per-form, because the embed
# isn't tied to one form — it's the user's whole inbox.
def _embed_allowed_origins() -> list[str]:
    raw = os.environ.get("FRONTFLOW_EMBED_ALLOWED_ORIGINS", "")
    if not raw.strip():
        return []
    return [o.strip() for o in raw.split(",") if o.strip()]


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


def can_view_submission(
    user: Optional["store.User"],
    submission: Submission,
    form: CompiledWorkflow,
) -> bool:
    """Whether a user may see a particular submission.

    Independent from form-level visibility (`can_access_form`) — a
    form may be public for filling but its submissions are private
    to their contributors. The two gates compose: form visibility
    decides "may you reach the form's filling URL", submission
    visibility decides "may you see this submission's data".

    Visibility rules, additive (any single one grants access):

      - admin                  → always
      - folder grant (any)     → always (forms inside that folder)
      - step submitter         → user_id appears on at least one
                                 step row of this submission
      - active assignee        → SubmissionAssignment row with
                                 user_id == user.id and
                                 revoked_at IS NULL
      - granter of an assign   → SubmissionAssignment row with
                                 granted_by_user_id == user.id

    Anonymous (`user is None`) always denied.

    For child submissions produced by `Assign`: the granter is the
    user whose action created the assignment row, so they see the
    child via the granter rule. The assignee sees it via the
    assignee rule. The original parent's submitters see only the
    PARENT, not the child — they're separate submissions.
    """
    if user is None:
        return False
    if getattr(user, "is_admin", False):
        return True
    # Folder grant (view or manage either grants visibility). The
    # folder path lives on the DB `Form` row, not on the compiled
    # workflow — look it up via store.form_folder().
    folder = store.form_folder(form.id) or ""
    if auth.user_form_access(user, folder) is not None:
        return True
    # Step submitter on this submission.
    for step in submission.steps:
        if getattr(step, "user_id", None) == user.id:
            return True
    # Active assignee or original granter on this submission. We
    # only count NON-revoked rows for the assignee path. For the
    # granter path we count all rows the user granted (revoking
    # an assignment shouldn't strip the granter's view — they
    # need to audit what they did, and they only see the
    # submission because their action created it). Two queries
    # keep the rule explicit.
    from sqlalchemy.orm import Session as _DBSession
    from sqlalchemy import select as _select
    from frontflow.dsl.store import (
        SubmissionAssignment as _Assignment, _engine,
    )
    with _DBSession(_engine) as db:
        # Active assignee.
        as_assignee = db.execute(
            _select(_Assignment).where(
                _Assignment.submission_handle == submission.handle,
                _Assignment.user_id == user.id,
                _Assignment.revoked_at.is_(None),
            )
        ).first()
        if as_assignee is not None:
            return True
        # Granter (active OR revoked — granters keep visibility for
        # audit; they're the actor who caused the submission to
        # exist for that role).
        as_granter = db.execute(
            _select(_Assignment).where(
                _Assignment.submission_handle == submission.handle,
                _Assignment.granted_by_user_id == user.id,
            )
        ).first()
    return as_granter is not None


def require_submission_visibility(
    form: CompiledWorkflow,
    submission: Submission,
    user: Optional["store.User"],
) -> None:
    """Raise HTTPException(404) if `user` may not view `submission`.

    404 (not 403) so a submission's existence isn't leaked by URL
    probing — same pattern as `can_access_form`. Admin and folder-
    grant routes bypass via `can_view_submission`'s rules.
    """
    if can_view_submission(user, submission, form):
        return
    raise HTTPException(
        status_code=404,
        detail=(
            "submission not found "
            "(or you don't have access to view it)"
        ),
    )


def _visible_submission_handles_for_user(
    form_id: str, user: "store.User",
) -> Optional[set[str]]:
    """Bulk-resolve the set of submission handles in `form_id` that
    `user` may view.

    Mirrors the per-submission rules in `can_view_submission` but in
    three bulk queries rather than O(rows) — meant for filtering a
    listing endpoint that otherwise would call `can_view_submission`
    once per row.

    Returns:
      - `None`    when the user sees every submission of this form
                  (admin, or a folder grant covering the form's folder)
                  — caller should skip filtering entirely.
      - `set[str]` of permitted submission handles otherwise. Empty
                  set means no visibility — the caller returns `[]`.

    Anonymous (`user is None`) is handled at the call site; this
    function requires a resolved user (matches the visibility-helper
    contract elsewhere).

    The three rules combined:
      - step submitter         — Step.user_id == user.id on any row
                                  of a submission in this form
      - active assignee        — SubmissionAssignment.user_id == user.id
                                  AND revoked_at IS NULL
      - granter of an assign   — SubmissionAssignment.granted_by_user_id
                                  == user.id (revoked OR active — see
                                  the `can_view_submission` docstring
                                  for the audit rationale)
    """
    # Admin / folder grant → no filtering needed. The caller returns
    # the listing as-is.
    if getattr(user, "is_admin", False):
        return None
    folder = store.form_folder(form_id) or ""
    if auth.user_form_access(user, folder) is not None:
        return None

    from sqlalchemy.orm import Session as _DBSession
    from sqlalchemy import select as _select
    from frontflow.dsl.store import (
        Submission as _Submission,
        Step as _Step,
        FormVersion as _FormVersion,
        SubmissionAssignment as _Assignment,
        _engine,
    )

    visible: set[str] = set()
    with _DBSession(_engine) as db:
        # Submitter — handles of any submission of this form that has
        # a Step row authored by this user.
        submitter_rows = db.execute(
            _select(_Submission.handle)
            .join(_FormVersion, _Submission.form_version_id == _FormVersion.id)
            .join(_Step, _Step.submission_handle == _Submission.handle)
            .where(
                _FormVersion.form_id == form_id,
                _Step.user_id == user.id,
            )
        ).all()
        visible.update(h for (h,) in submitter_rows)

        # Active assignee — handles where this user holds an
        # un-revoked grant on a submission of THIS form. Join through
        # Submission + FormVersion since the assignment row has no
        # form_id column (it links to a submission_handle).
        assignee_rows = db.execute(
            _select(_Assignment.submission_handle)
            .join(
                _Submission,
                _Submission.handle == _Assignment.submission_handle,
            )
            .join(
                _FormVersion,
                _Submission.form_version_id == _FormVersion.id,
            )
            .where(
                _FormVersion.form_id == form_id,
                _Assignment.user_id == user.id,
                _Assignment.revoked_at.is_(None),
            )
        ).all()
        visible.update(h for (h,) in assignee_rows)

        # Granter — handles where this user issued the grant (active
        # or revoked) on a submission of this form. Same join shape
        # as above; granters keep audit visibility forever.
        granter_rows = db.execute(
            _select(_Assignment.submission_handle)
            .join(
                _Submission,
                _Submission.handle == _Assignment.submission_handle,
            )
            .join(
                _FormVersion,
                _Submission.form_version_id == _FormVersion.id,
            )
            .where(
                _FormVersion.form_id == form_id,
                _Assignment.granted_by_user_id == user.id,
            )
        ).all()
        visible.update(h for (h,) in granter_rows)

    return visible


def _token_bears_submission(
    token: Optional[str], submission_handle: str,
) -> bool:
    """True if `token` is a valid signed link for this submission
    handle. Used by submission-read endpoints as a bypass for the
    `require_submission_visibility` gate — a token-bearer was
    granted access to the specific submission by the link issuer,
    so they don't also need to be on the contributor list.
    """
    if not token:
        return False
    from frontflow.dsl import signed_links as _signed_links
    payload = _signed_links.verify(
        token, submission_handle=submission_handle,
    )
    return payload is not None


def require_workspace_visibility(
    workspace_id: str,
    frontflow_session: str | None = Cookie(default=None),
    key: str | None = None,
) -> None:
    """FastAPI dependency — gate a workspace by its visibility.

    Mirrors `require_form_visibility`, including the 404-not-403: a
    restricted workspace's existence is not leaked by URL probing.

    This is also the gate on the workspace's dashboard panels. A
    dashboard inside a form inherits that form's ACL; a dashboard in a
    workspace has none to inherit, so the workspace's own visibility is
    what authorizes it.
    """
    user = auth.resolve_session(frontflow_session)
    if not auth.can_access_workspace(user, workspace_id, key):
        raise HTTPException(
            status_code=404, detail=f"workspace {workspace_id!r} not found"
        )


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


def _check_form_visibility_with_token(
    form_id: str,
    submission_handle: str,
    frontflow_session: str | None,
    key: str | None,
    token: str | None,
) -> None:
    """Token-aware variant of require_form_visibility for endpoints
    that accept signed-link tokens. Bypass order:

      1. If the existing visibility check passes (public, unlisted+key,
         restricted+permitted-user, admin) → allow.
      2. Otherwise if `token` verifies against this submission_handle,
         the bearer IS authorized to reach this submission specifically.
         Allow form-level access on that basis — the role gate still
         decides what the bearer can do.
      3. Neither path passes → 404.

    Called inline by read_step / submit_step instead of
    `Depends(require_form_visibility)` since those endpoints also
    accept a `token` query param.
    """
    user = auth.resolve_session(frontflow_session)
    if auth.can_access_form(user, form_id, key):
        return
    if token:
        from frontflow.dsl import signed_links as _signed_links
        payload = _signed_links.verify(
            token, submission_handle=submission_handle,
        )
        if payload is not None:
            return
    raise HTTPException(
        status_code=404, detail=f"form {form_id!r} not found"
    )


def _resolve_user_roles_on_submission(
    submission_handle: str,
    frontflow_session: str | None,
    token: str | None = None,
) -> tuple[frozenset[str], bool, Optional[int]]:
    """Read the session cookie and return the role set + admin flag
    + user_id for the runtime auth check (Phase 1's
    `resolve_node_access` / `resolve_field_access`).

    Three-tuple:
      (roles_held_on_this_submission, is_admin, user_id_or_None)

    Resolution order:
      1. The frontflow_session cookie — a logged-in user.
      2. A signed-link `token` query param (Phase 5.5). A valid
         token authenticates as the token's `user_id` AND
         requires that user to still hold an active assignment
         on this submission. Without the assignment check, an
         old token would survive revocation — the design
         requires both signature integrity AND current state.

    A request with no usable credential returns (empty, False,
    None) — the auth check then either grants the open-mode
    default (form has no roles) or renders pending.

    Admins always get `is_admin=True` regardless of assignments —
    short-circuits the role check to "fully permitted."
    """
    from frontflow.dsl import assignments as _assignments
    user = auth.resolve_session(frontflow_session)
    if user is None and token:
        # Fall back to the signed-link token. Verify integrity,
        # binding, and expiry; then verify the user is still
        # assigned on this submission (revocation invalidates a
        # still-valid token).
        from frontflow.dsl import signed_links as _signed_links
        from frontflow.dsl.store import User, _engine
        from sqlalchemy.orm import Session as _DBSession
        payload = _signed_links.verify(
            token, submission_handle=submission_handle,
        )
        if payload is not None:
            token_user_id = payload["user_id"]
            with _DBSession(_engine) as _db:
                token_user = _db.get(User, token_user_id)
            # Token user must exist AND still be active. An
            # inactive user's token is no longer usable.
            if (
                token_user is not None
                and token_user.is_active
            ):
                # Token-authenticated users are NEVER treated as
                # admins (the token's scope is the assignment, not
                # the full admin surface). Skip the admin
                # short-circuit and read assignment state.
                roles = (
                    _assignments
                    .active_roles_for_user_on_submission(
                        submission_handle, token_user_id,
                    )
                )
                if roles:
                    return roles, False, token_user_id
                # Token verified but assignment revoked — treat
                # as anonymous so the auth check renders pending.
    if user is None:
        return frozenset(), False, None
    if getattr(user, "is_admin", False):
        return frozenset(), True, user.id
    roles = _assignments.active_roles_for_user_on_submission(
        submission_handle, user.id,
    )
    return roles, False, user.id


# The live registry, keyed by form_id. Rebound wholesale by each scan so
# in-flight requests always read a complete, consistent snapshot.
FORMS: dict[str, CompiledWorkflow] = {}

# Per-file / per-workflow load failures from the most recent scan.
LOAD_ERRORS: dict[str, str] = {}

# form_id -> the id of the form_version that is currently live (the
# latest compiled state). New submissions are pinned to this.
FORM_VERSION_IDS: dict[str, int] = {}

# form_id -> (folder, source). Kept in sync with the most recent scan.
# Used by the Source-tab fallback path on `read_form_source` when a
# form is loaded into FORMS but its DB upsert raised (so it isn't in
# FORM_VERSION_IDS). Previously this lived only as a local in
# `scan_workflows`, which caused a NameError when the fallback fired.
FORM_SOURCES: dict[str, tuple[str, str]] = {}

# form_version id -> its executable CompiledWorkflow. The live versions
# are seeded from FORMS; older versions are recompiled on demand from
# their stored DSL source so an in-flight submission always advances on
# the version it began. An old version's compiled form never changes,
# so cache entries stay valid across rescans.
_VERSION_WF_CACHE: dict[int, CompiledWorkflow] = {}
_compile_lock = threading.Lock()


def _exec_form_source(name: str, source: str, path=None, uri=None) -> None:
    """Execute one workflow file's source text. Running the module body
    registers its @form workflows into WORKFLOWS via the decorator's
    trailing call. `name` is the file's source-relative name, used to
    derive a unique module name.

    `uri` is the file's canonical origin — an absolute path for local
    sources, an `s3://bucket/key` URI for S3 ones. It becomes the
    module's `__file__` (and the compile filename, so tracebacks name
    the real origin), which is what `frontflow.Assets(__file__)`
    anchors on to read sibling files from either kind of source.
    `path` is the local-filesystem fallback; failing both, the
    source-relative name is used."""
    mod_name = _mod_name(name)
    module = importlib.util.module_from_spec(
        importlib.util.spec_from_loader(mod_name, loader=None)
    )
    filename = str(uri or path or name)
    module.__file__ = filename
    sys.modules[mod_name] = module  # registered before exec for self-refs
    code = compile(source, filename=filename, mode="exec")
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
    stored source.

    When stored source fails to re-exec — broken imports, API drift,
    deleted helper modules, anything — we fall back to deserializing
    the form_version's `compiled_graph` JSON. The result is a
    *view-only* CompiledWorkflow: full layout/fields/buttons/node-graph
    intact, but backend callables replaced with placeholders that raise
    `WorkflowSourceUnavailable` if invoked. This keeps submissions
    auditable across breaking changes — a property the system must
    have because submissions outlive any single source revision.

    Cached — both successful recompilation AND fallback deserialization
    happen at most once per form_version.
    """
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
        try:
            wf = compile_source(fv["source"], form_id)
        except Exception as compile_err:  # noqa: BLE001
            # Source can't re-exec — typically because the library or
            # helper APIs the source imports have changed. Fall back to
            # the serialized compiled_graph for view-only access.
            err_msg = f"{type(compile_err).__name__}: {compile_err}"
            wf = compiled_graph_to_workflow(
                fv["compiled_graph"], source_error=err_msg,
            )
    _VERSION_WF_CACHE[form_version_id] = wf
    return wf


# workspace_id -> its compiled panel tree. Rebuilt on every scan.
WORKSPACE_LAYOUTS: dict[str, dict[str, Any]] = {}


def _scan_workspaces(
    compiled_forms: dict[str, CompiledWorkflow],
    errors: dict[str, str],
) -> None:
    """Compile registered workspaces and upsert their visibility rows.

    A workspace naming a form that does not exist is a load error for
    that workspace only — a typo should not take down the forms it sits
    beside, and the panel would otherwise render as a silent blank.
    """
    from frontflow.dsl.workspaces import compile_workspace

    WORKSPACE_LAYOUTS.clear()

    for ws_id, ws in list(WORKSPACES.items()):
        try:
            layout = compile_workspace(ws)
        except Exception as e:  # noqa: BLE001
            errors[ws_id] = f"workspace compile failed — {type(e).__name__}: {e}"
            print(f"[workspace] {ws_id}: compile failed — {e}")
            continue

        missing = [
            panel_form_id
            for panel_form_id in _workspace_form_ids(layout["layout"])
            if panel_form_id not in compiled_forms
        ]
        if missing:
            message = (
                f"workspace {ws_id!r} references unknown form(s): "
                f"{', '.join(sorted(missing))}"
            )
            errors[ws_id] = message
            print(f"[workspace] {message}")
            continue

        WORKSPACE_LAYOUTS[ws_id] = layout
        try:
            store.upsert_workspace(
                workspace_id=ws_id, name=ws.title, private=ws.private
            )
        except Exception as e:  # noqa: BLE001 — persistence must not break serving
            print(f"[workspace] {ws_id}: persistence failed — {e}")

    try:
        store.mark_workspaces_not_live(set(WORKSPACE_LAYOUTS))
    except Exception as e:  # noqa: BLE001
        print(f"[workspace] liveness update failed — {e}")

    if WORKSPACE_LAYOUTS:
        print(f"[workspace] serving workspaces: {sorted(WORKSPACE_LAYOUTS)}")


def _workspace_form_ids(block: dict[str, Any]) -> list[str]:
    """Every form id a compiled workspace tree references."""
    found: list[str] = []
    if block.get("type") == "workspace_form":
        form_id = (block.get("props") or {}).get("form_id")
        if form_id:
            found.append(form_id)
    for child in block.get("children") or []:
        found.extend(_workspace_form_ids(child))
    return found


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
    WORKSPACES.clear()

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
            _exec_form_source(
                wf_file.name, wf_file.source,
                path=getattr(wf_file, "path", None),
                uri=getattr(wf_file, "uri", None),
            )
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
            # A form declared `private=True` at @form is enforced on
            # every scan — the DSL is the source of truth for
            # visibility. Admin UI cannot override DSL settings.
            dsl_visibility: Optional[str] = None
            if getattr(wf, "private", False):
                dsl_visibility = "restricted"
            result = store.upsert_form_version(
                form_id=wf_id,
                name=cw.title or wf_id,
                folder_path=folder,
                compiled_graph=serialized,
                content_hash=workflow_content_hash(serialized),
                source=source,
                dsl_visibility=dsl_visibility,
            )
            version_ids[wf_id] = result.form_version_id
            _VERSION_WF_CACHE[result.form_version_id] = cw  # seed the live version's cache

            # Auto-repin on minor bumps when configured. Resolution
            # order: form-level `auto_repin_minor` (if explicitly set,
            # either way) overrides the env default
            # `FRONTFLOW_AUTO_REPIN_MINOR`. Only in-flight submissions
            # are touched — terminated/failed ones stay on their
            # pinned version for historical fidelity.
            if result.bump == "minor" and _should_auto_repin_minor(wf):
                migrated = store.auto_repin_minor_submissions(
                    form_id=wf_id,
                    major_version=result.version,
                    new_form_version_id=result.form_version_id,
                )
                if migrated > 0:
                    print(
                        f"[workflow] {wf_id}: auto-repinned {migrated} "
                        f"in-flight submission(s) to v{result.version}"
                        f".{result.minor_version}"
                    )
        except Exception as e:  # noqa: BLE001 — persistence must not break serving
            print(f"[workflow] {wf_id}: version persistence failed — {e}")

    store.mark_forms_live(set(compiled))
    FORMS, LOAD_ERRORS, FORM_VERSION_IDS = compiled, errors, version_ids

    # Workspaces last: one names forms, so the form registry has to be
    # complete before a workspace's references can be checked.
    _scan_workspaces(compiled, errors)
    # Mirror the per-form source/folder cache to module scope so the
    # `read_form_source` fallback (and any other out-of-scan reader)
    # can find a form's source even when its DB upsert raised. Using
    # .clear()+.update() rather than reassignment so existing reads
    # observe the same dict identity.
    FORM_SOURCES.clear()
    FORM_SOURCES.update(form_meta)

    # Cross-form validation pass — every Assign operator's child
    # form, role, and picker reference are checked against the
    # now-complete registry. Failures degrade the offending form to
    # a load error rather than tearing down the whole scan.
    from frontflow.dsl.compile import validate_assign_references
    for wf_id, cw in list(FORMS.items()):
        try:
            validate_assign_references(cw, FORMS)
        except ValueError as e:
            LOAD_ERRORS[wf_id] = f"Assign validation failed — {e}"
            FORMS.pop(wf_id, None)
            FORM_VERSION_IDS.pop(wf_id, None)

    summary = f"[workflow] serving forms: {sorted(FORMS)}"
    if LOAD_ERRORS:
        summary += f" | load errors: {sorted(LOAD_ERRORS)}"
    print(summary)
    return FORMS


def hydrate_state() -> None:
    """Rehydrate persisted submissions into the runtime's in-memory
    working set. Run once at startup, after the workflow scan.

    Two passes:

      1. Seed the runtime's `_id_index` from EVERY persisted submission
         id, regardless of whether its workflow can be recompiled.
         This closes a long-standing blind spot: an un-hydratable
         submission (DSL rename, removed source file, …) used to leave
         its submission_id absent from the in-memory index, so a fresh
         submission could mint a colliding id and fail at the DB
         UNIQUE constraint instead of at the clean in-memory check.
         The fix is cheap — one read of `(submission_id, handle)`
         pairs — and makes the failure mode consistent across
         hydratable / un-hydratable rows.

      2. The full hydrate loop: for each submission whose workflow we
         CAN recompile, restore its in-memory state. Un-hydratable
         ones are skipped with a single log line (down from one per
         submission, which spammed logs on installs with lots of
         stale rows).
    """
    # Pass 1: id-index priming — happens unconditionally so the
    # collision guard is intact even when no submissions hydrate.
    persisted_ids = store.list_all_submission_ids()
    if persisted_ids:
        with runtime._submissions_lock:
            for submission_id, handle in persisted_ids.items():
                # Defensive: don't clobber an in-memory entry if it
                # exists (an already-hydrated submission this pass
                # would have its handle set; the persisted handle
                # should match it).
                runtime._id_index.setdefault(submission_id, handle)

    # Pass 2: full hydrate — re-execs the form source for each
    # submission's pinned form_version_id. Skipping a submission
    # here doesn't lose its id (Pass 1 already claimed it).
    # Pass 2: full hydrate. `resolve_workflow` falls back to
    # deserializing the stored `compiled_graph` when source can't
    # re-exec — the resulting workflow is view-only (backend
    # callables become inert placeholders) but enough to render
    # the submission's detail page and history.
    #
    # No submission is ever skipped here. Skipping makes the
    # submission unreachable through the in-memory cache and turns
    # an audit-trail problem into a "submission not found" UX. If
    # `resolve_workflow` itself raises (a programming error, not a
    # source-compile error — the deserialize fallback handles
    # those), we log per-submission and continue so one bad row
    # doesn't take out the whole boot.
    loaded = 0
    source_unavailable: list[tuple[str, str]] = []  # (display_id, error)
    failed: list[tuple[str, str]] = []  # (display_id, error) — truly unreachable
    for snap in store.load_submissions():
        display_id = (
            snap.get("submission_id") or snap.get("handle") or "<unknown>"
        )
        try:
            wf = resolve_workflow(snap["form_version_id"])
        except Exception as e:  # noqa: BLE001
            failed.append((display_id, f"{type(e).__name__}: {e}"))
            continue
        hydrate_submission(snap, form_id=wf.id)
        loaded += 1
        if getattr(wf, "source_unavailable", False):
            source_unavailable.append(
                (display_id, getattr(wf, "source_error", "") or "")
            )
    if loaded:
        print(f"[workflow] rehydrated {loaded} submission(s)")
    if source_unavailable:
        # Source can't re-exec but the compiled_graph deserialized
        # cleanly — these submissions are viewable but cannot advance.
        # Naming them lets admins go straight to the affected URLs
        # rather than spelunking the DB.
        print(
            f"[workflow] {len(source_unavailable)} submission(s) "
            "are view-only — pinned form source no longer compiles, "
            "rendered from stored compiled_graph instead:"
        )
        for display_id, err in source_unavailable:
            print(f"[workflow]   - {display_id} ({err})")
    if failed:
        # True failures (the deserialize fallback ALSO failed, or
        # something else broke). These are unreachable; log them
        # loudly so they don't hide.
        print(
            f"[workflow] {len(failed)} submission(s) could not be "
            "rehydrated at all (compiled_graph deserialization also "
            "failed) — these are unreachable until repaired:"
        )
        for display_id, err in failed:
            print(f"[workflow]   - {display_id} ({err})")


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


@app.middleware("http")
async def _security_headers(request, call_next):
    """Attach security headers on every response.

    The main concern is iframe embedding: by default every response
    carries `Content-Security-Policy: frame-ancestors 'none'` plus
    the legacy `X-Frame-Options: DENY`. A form whose `@form` decorator
    sets `iframe_allowed_origins=[...]` is the exception — that
    form's render path gets a permissive `frame-ancestors` directive
    naming the allowed origins, so the browser permits the embed.

    Two extra rules:
      - Only `public` forms are actually iframable. A form with
        `iframe_allowed_origins` set but a current visibility of
        `unlisted` or `restricted` is served `frame-ancestors 'none'`
        anyway, with a warning logged. This lets an admin tighten
        a form's visibility without re-deploying code to disable
        embedding.
      - The permissive directive applies only to the form-rendering
        route(s); the admin UI itself, API endpoints, and login
        flow always get `'none'` regardless.
    """
    response = await call_next(request)
    # The single-page-app catches `/forms/{form_id}` (and child paths
    # under it like `/forms/{form_id}/submissions/{handle}`) so they
    # all render the SPA shell. The SPA reads its route, detects
    # iframe context, and toggles embedded mode. We key the
    # iframe-permissive CSP on the form_id parsed out of the path.
    path = request.url.path
    permitted: Optional[list[str]] = None
    # /embed/* paths (e.g., /embed/my-tasks, /api/embed/my-tasks) use
    # the install-wide embed allowlist, NOT a per-form list — they're
    # not tied to a single form.
    if path.startswith("/embed/") or path.startswith("/api/embed/"):
        embed_origins = _embed_allowed_origins()
        if embed_origins:
            permitted = embed_origins
    else:
        form_id = _iframe_form_id_for_path(path)
        if form_id is not None:
            wf = FORMS.get(form_id)
            if wf is not None and wf.iframe_allowed_origins:
                # Visibility gate. Lookup is cheap; a single get on the
                # forms table. Bypassed gracefully if the DB is
                # momentarily unreachable — falls through to deny.
                try:
                    from frontflow.dsl import store as _store_mod
                    with _store_mod._engine.connect() as conn:
                        from sqlalchemy import select as _select
                        from frontflow.dsl.store import Form as _Form
                        row = conn.execute(
                            _select(_Form.visibility).where(
                                _Form.form_id == form_id
                            )
                        ).first()
                    visibility = (row[0] if row else None) or "public"
                except Exception:
                    visibility = "public"
                if visibility == "public":
                    permitted = wf.iframe_allowed_origins
                else:
                    import logging
                    logging.getLogger("frontflow").warning(
                        "form %r has iframe_allowed_origins but visibility "
                        "is %r; serving with frame-ancestors 'none' "
                        "(only public forms can be embedded)",
                        form_id, visibility,
                    )
    if permitted:
        response.headers["Content-Security-Policy"] = (
            "frame-ancestors " + " ".join(permitted)
        )
        # Drop the legacy header — it can't express an allowlist,
        # and a UA that supports CSP `frame-ancestors` will ignore
        # `X-Frame-Options` anyway when both are present, but some
        # older UAs preferentially honor the legacy header.
        if "X-Frame-Options" in response.headers:
            del response.headers["X-Frame-Options"]
    else:
        response.headers["Content-Security-Policy"] = (
            "frame-ancestors 'none'"
        )
        response.headers["X-Frame-Options"] = "DENY"
    return response


def _iframe_form_id_for_path(path: str) -> Optional[str]:
    """Extract the form_id from a SPA path if it's a form-rendering
    (live-form) route, else None.

    The SPA's live-form URLs are `/forms/{form_id}/form` and child
    paths under it (`/forms/{form_id}/form/draft`,
    `/forms/{form_id}/form/submission/{handle}`, …). Anything else —
    the admin listing `/forms`, summary pages `/forms/{form_id}`
    without `/form`, the API, the login page, static assets — is
    never iframable.
    """
    if not path.startswith("/forms/"):
        return None
    rest = path[len("/forms/"):]
    # Strip query string defensively, although the path usually
    # arrives without it via request.url.path.
    rest = rest.split("?", 1)[0]
    parts = rest.split("/")
    # /forms/<form_id>/form[/...] is iframable. /forms/<form_id> on
    # its own (admin summary) is not.
    if len(parts) < 2:
        return None
    form_id, segment = parts[0], parts[1]
    if not form_id or segment != "form":
        return None
    return form_id

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


class AssignedChild(BaseModel):
    """One spawned child submission shown inline on the parent's
    chain — the parent's graph view renders these as a clickable
    chip under the node that spawned them.

    Visible because the parent's Assign operator fired and granted
    `assignee_username` the role `role_id` on the child submission
    identified by `submission_handle` (or `submission_id` once
    minted). `revoked_at` is non-null if the grant has since been
    revoked; the frontend can still link to the child for audit
    but should style differently.
    """
    assignment_id: int
    child_form_id: str
    child_form_title: str
    child_submission_handle: str
    child_submission_id: Optional[str] = None
    child_submission_state: str
    role_id: str
    assignee_user_id: int
    assignee_username: Optional[str] = None
    granted_at: datetime
    revoked_at: Optional[datetime] = None


class TaskInstance(BaseModel):
    # One row in the submission's chain.
    #   kind="hitl"     — a HITL node (a form screen)
    #   kind="external" — an ExternalTask (Airflow poll, etc.)
    #   kind="backend"  — a workflow-level @backend step
    task_id: str
    state: str
    is_hitl: bool
    kind: str = "hitl"
    # Presentational backend-step grouping (`with backend_group(...)`)
    # — consecutive backend tasks sharing group_id collapse into one
    # chain-UI node titled group_title.
    group_id: Optional[str] = None
    group_title: Optional[str] = None
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
    # Developer-supplied message rendered in the status panel while
    # the operator is in a non-terminal state (queued/running/awaiting).
    # Templated against the form's `steps` namespace; resolved on each
    # advance. Null when no `waiting_message` was set on the operator.
    waiting_message: Optional[str] = None
    # Whether a user may rerun *this step on its own* from the UI.
    # False suppresses the per-step rerun menu; an upstream cascade can
    # still clear the step. HITL/form nodes are always retryable.
    retryable: bool = True
    # Per-operator polling rate hint (ms). Frontend uses the minimum
    # across all in-flight external tasks as its refetch interval —
    # an operator that wants slower polling can declare it. None →
    # operator opts into the framework default. Always null for HITL
    # and backend tasks (they don't drive polling).
    poll_interval_ms: Optional[int] = None
    # Per-step error message. Populated when the step's backend (or a
    # chain step) raised — `state` is "failed". Surfaced in the chain
    # UI so the user sees the exact exception (e.g. "ValueError: ...")
    # without digging through server logs. Null on non-failed steps.
    error: Optional[str] = None
    # A dashboard refresh requested by a `superset.RefreshDashboard`
    # operator at this point in the chain: `{dashboard, time_range,
    # token}`. An open dashboard block re-queries its charts in place
    # when it sees a token it has not handled. Null on every other task.
    dashboard_refresh: Optional[dict] = None
    dashboard_filters: Optional[dict] = None
    # Full Python traceback paired with `error`. The chain UI renders
    # this in a collapsible details panel under the short message.
    # Null on non-failed steps and on rows persisted before the
    # column existed.
    traceback: Optional[str] = None
    # Children spawned from this task by an Assign operator. Empty for
    # tasks that didn't fire any Assign, or whose Assigns produced no
    # grants (e.g. picker resolved to nobody). Each entry is one
    # SubmissionAssignment row joined with the parent submission for
    # context — the frontend renders these as inline chips under the
    # parent node with click-through links to the child submission.
    assignments: list[AssignedChild] = []


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
    # The submission's pinned form version (human-facing integer), and
    # the form's current live version. When `live_form_version >
    # form_version`, the submission lags the form and an admin can
    # re-pin it. Surfaced here so the edit/reset modal can offer "use
    # latest form version" inline rather than making the user navigate
    # to the submission summary to re-pin separately.
    form_version: int
    live_form_version: int
    # Minor (source-only) version counterparts. Zero when the form
    # has never had a non-structural revision since the matching
    # major. The frontend compares on the full (major, minor) tuple
    # so a minor-only difference (a body-only code edit that didn't
    # change the compiled-graph hash) still surfaces "use latest" in
    # the modal — important when auto-repin-minor is off and an
    # admin wants the live source applied to this submission from
    # the edit dialog they're already in.
    form_minor_version: int = 0
    live_minor_version: int = 0


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
    # The page this step belongs to, when it's a section node inside a
    # page. Null for top-level nodes. The frontend uses these to render
    # the page as its own view.
    page_id: Optional[str] = None
    page_title: Optional[str] = None
    # When a chain step downstream of this node's submit failed — a
    # backend that raised, an operator that errored — the failure
    # message lands here. Null when the step's chain hasn't failed.
    # Surfaced on the HITL node card so the user sees what went wrong
    # without digging into a separate details page.
    error: Optional[str] = None
    # Per-request access decision — the runtime auth check's verdict
    # for THIS user on THIS node. Frontend renders accordingly:
    #   {"can_read": true,  "can_write": true,  "pending": false} → normal
    #   {"can_read": true,  "can_write": false, "pending": false} → read-only
    #   {"can_read": false, "can_write": false, "pending": true,
    #    "missing_write_roles": ["recruiter"]} → pending placeholder
    # Open-mode forms (no roles declared) always return the first
    # shape — backward-compatible. The default {} means "no role
    # gate applied" which the frontend treats as fully permitted.
    access: dict[str, Any] = Field(default_factory=dict)


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
    # Content-column widths for the form-facing views. Any CSS length.
    # Defaults keep themes stored before these fields existed valid.
    nodeWidth: str = "56rem"
    pageWidth: str = "80rem"


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


class CommentOut(BaseModel):
    """One comment in a component thread."""
    id: int
    thread_id: str
    author: str
    user_id: Optional[str] = None
    body: str
    created_at: datetime


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=10_000)
    # Display name for anonymous commenters; an authenticated
    # session's username wins over this.
    author: Optional[str] = None


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


def _require_source_available(workflow: CompiledWorkflow) -> None:
    """Refuse the request with 409 when the submission's pinned form
    source no longer compiles. Used by every endpoint that would run
    backends — submit_step, hitl response, clear, advance, etc.

    Submissions are still VIEWABLE in this state (the deserialize
    fallback gave us a structurally complete CompiledWorkflow), but
    no forward progress is possible because backends are inert
    placeholders. The clean failure mode is a clear 409 with the
    original compile error attached, so the UI can surface a banner
    and the client knows the state isn't transient.

    The repin endpoint is intentionally exempt — repin doesn't need
    the OLD source to compile, only the NEW one. That's the recovery
    path users take to bring a source-unavailable submission back
    to life.
    """
    if getattr(workflow, "source_unavailable", False):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "form_source_unavailable",
                "message": (
                    "this submission's pinned form source no longer "
                    "compiles; advancement is disabled until the form "
                    "source is repaired or the submission is re-pinned "
                    "to a compatible version"
                ),
                "source_error": getattr(workflow, "source_error", "") or "",
            },
        )


def _persist(workflow: CompiledWorkflow, submission: Submission) -> None:
    """Write the submission's current state through to the database.

    A no-op while the submission is still a session draft — persistence
    begins the moment its id is minted. Called after every runtime
    operation; failures are logged, never surfaced to the request.

    Also a no-op for PREVIEW submissions: by definition, preview state
    must never reach the database. This is the choke point — every DB
    write path goes through here, so the preview guard is centralized.
    """
    if submission.preview:
        return
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


def _load_assignments_granted_by(
    parent_submission_handle: str,
) -> dict[str, list[AssignedChild]]:
    """Fetch every SubmissionAssignment row granted by this parent
    submission, grouped by the parent node that fired the Assign.
    Joins are bulk-batched (one query per table) so the parent's
    graph-view render stays O(1) per task — not per child.

    Returns `{parent_node_id: [AssignedChild, ...]}`. A node with no
    fired Assigns gets no entry (callers fall back to empty list).
    """
    from frontflow.dsl.store import (
        SubmissionAssignment as _Assignment,
        Submission as _SubmissionRow,
        User as _User,
        Form as _FormRow,
        FormVersion as _FormVersionRow,
        _engine,
    )
    from sqlalchemy import select as _select
    from sqlalchemy.orm import Session as _DBSession

    with _DBSession(_engine) as db:
        rows = db.execute(
            _select(_Assignment).where(
                _Assignment.granted_by_submission_handle == (
                    parent_submission_handle
                ),
            ).order_by(_Assignment.granted_at)
        ).scalars().all()
        if not rows:
            return {}

        # Bulk-fetch child submissions for state + minted id.
        child_handles = [r.submission_handle for r in rows]
        sub_rows = db.execute(
            _select(_SubmissionRow).where(
                _SubmissionRow.handle.in_(child_handles),
            )
        ).scalars().all()
        sub_by_handle = {s.handle: s for s in sub_rows}

        # Bulk-fetch child form titles via form_version → form.
        version_ids = list({
            s.form_version_id for s in sub_rows
            if s.form_version_id is not None
        })
        version_to_form_id: dict[int, str] = {}
        if version_ids:
            for fv in db.execute(
                _select(_FormVersionRow).where(
                    _FormVersionRow.id.in_(version_ids),
                )
            ).scalars().all():
                version_to_form_id[fv.id] = fv.form_id

        form_ids = set(version_to_form_id.values())
        form_titles: dict[str, str] = {}
        if form_ids:
            for fr in db.execute(
                _select(_FormRow).where(_FormRow.form_id.in_(form_ids))
            ).scalars().all():
                form_titles[fr.form_id] = fr.name or fr.form_id

        # Bulk-fetch assignee usernames.
        user_ids = {r.user_id for r in rows}
        usernames: dict[int, str] = {}
        if user_ids:
            for u in db.execute(
                _select(_User).where(_User.id.in_(user_ids))
            ).scalars().all():
                usernames[u.id] = u.username

    out: dict[str, list[AssignedChild]] = {}
    for r in rows:
        sub = sub_by_handle.get(r.submission_handle)
        if sub is None:
            # Orphan grant — parent_submission row likely deleted.
            # Skip rather than crash; admin/audit can still find the
            # row via the user-detail page.
            continue
        # The parent node id lives on the CHILD submission row
        # (it's the node that fired the Assign that produced this
        # child). Falls back to "" so a row missing the
        # back-reference still groups (under the unknown-node
        # bucket) rather than crashes.
        parent_node = sub.parent_assign_node_id or ""
        child_form_id = (
            version_to_form_id.get(sub.form_version_id)
            if sub.form_version_id is not None
            else ""
        ) or ""
        out.setdefault(parent_node, []).append(
            AssignedChild(
                assignment_id=r.id,
                child_form_id=child_form_id,
                child_form_title=form_titles.get(
                    child_form_id, child_form_id,
                ),
                child_submission_handle=sub.handle,
                child_submission_id=sub.submission_id,
                child_submission_state=sub.state or "unknown",
                role_id=r.role_id,
                assignee_user_id=r.user_id,
                assignee_username=usernames.get(r.user_id),
                granted_at=r.granted_at,
                revoked_at=r.revoked_at,
            )
        )
    return out


def _build_tasks(
    submission: Submission, form: CompiledWorkflow
) -> list[TaskInstance]:
    """Flatten the submission's state into the task list the frontend's
    chain-segmentation logic consumes.

    Each step becomes a task: HITL nodes → kind="hitl", backend steps →
    kind="backend" (hidden backend steps are omitted entirely),
    ExternalTask operators → kind="external" with state derived from
    elapsed time since the form was submitted. HITL tasks that fired
    an Assign operator carry their spawned child submissions in
    `assignments` so the parent's graph view shows the per-role
    children inline.
    """
    # Bulk-fetch every assignment row granted by this submission, in
    # one query, before walking the steps. Mapped by parent node so
    # each HITL task can attach its slice in O(1). The same rows are
    # also joined with the parent submission and User tables for
    # display labels (form title, assignee username, etc.).
    assignments_by_node = _load_assignments_granted_by(
        submission.handle
    )

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
                    group_id=getattr(step_def, "group_id", None),
                    group_title=getattr(step_def, "group_title", None),
                    status=step.status.key,
                    retryable=step_def.fn.retryable,
                    error=step.error,
                    traceback=step.traceback,
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
                assignments=assignments_by_node.get(step.node_id, []),
                error=step.error,
                traceback=step.traceback,
            )
        )

        # `external_state` as well as `is_submitted`: a node declared
        # `closes=False` runs its chain and then deliberately stays
        # unsubmitted, so gating on submission alone would hide the very
        # operators it exists to drive. An ordinary awaiting step has no
        # external_state, so the walk below stops on its first task
        # exactly as before.
        if (
            step.is_submitted or step.external_state
        ) and not step.branch_taken_explicitly:
            ext_tasks = step_def.external_tasks
            # Both real and mock dispatch write per-operator state to
            # `step.external_state` — walk it uniformly. A task with no
            # entry yet hasn't been reached this tick; stop there
            # (later tasks haven't started either).
            for ext in ext_tasks:
                st = step.external_state.get(ext.task_id)
                if st is None:
                    break  # not reached yet
                state = st.get("state", "queued")
                # An Airflow HITL task waiting on a person carries the
                # prompt for the form to render.
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
                        dashboard_refresh=st.get("dashboard_refresh"),
                        dashboard_filters=st.get("dashboard_filters"),
                        waiting_message=st.get("waiting_message"),
                        retryable=ext.retryable,
                        poll_interval_ms=ext.poll_interval_ms,
                    )
                )
                if state != "success":
                    break  # nothing after a non-terminal task
    return tasks


def _build_submission_response(
    submission: Submission, form: CompiledWorkflow
) -> SubmissionResponse:
    # Translate the version id pinned on this submission to its
    # human-facing integer the same way `read_submission` does. Same
    # for the live version. The numbers are needed so the edit/reset
    # modal can offer "use latest form" inline (admin-only) — the
    # modal compares the two and only shows the option when the
    # submission lags. A 0 fallback means "the form_version row was
    # somehow deleted" — degrades to "we don't know," and the modal
    # treats lag as zero.
    fv = store.get_form_version(submission.form_version_id)
    active_version_num = fv["version"] if fv is not None else 0
    active_minor_num = fv["minor_version"] if fv is not None else 0
    live_version_id = FORM_VERSION_IDS.get(form.id)
    if live_version_id is None:
        live_version_num = active_version_num
        live_minor_num = active_minor_num
    else:
        live_fv = store.get_form_version(live_version_id)
        live_version_num = (
            live_fv["version"] if live_fv is not None
            else active_version_num
        )
        live_minor_num = (
            live_fv["minor_version"] if live_fv is not None
            else active_minor_num
        )
    return SubmissionResponse(
        handle=submission.handle,
        submission_id=submission.submission_id,
        form_id=submission.form_id,
        state=_submission_state(submission),
        started_at=submission.started_at.isoformat(),
        tasks=_build_tasks(submission, form),
        form_version=active_version_num,
        live_form_version=live_version_num,
        form_minor_version=active_minor_num,
        live_minor_version=live_minor_num,
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


@api.get(
    "/forms/{form_id}/submissions/{submission_id}/comments/{thread_id}",
    response_model=list[CommentOut],
    dependencies=[Depends(require_form_visibility)],
)
def read_comments(
    form_id: str, submission_id: str, thread_id: str,
    frontflow_session: str | None = Cookie(default=None),
    token: str | None = None,
) -> list[CommentOut]:
    """A component thread's comments, oldest first. Threads are named
    by `displays.Comments` block ids today; the (form, submission,
    thread) key is deliberately generic so future anchors (per-block
    annotations, field-level notes) reuse the same storage."""
    form, submission = _get_submission_or_404(form_id, submission_id)
    if not _token_bears_submission(token, submission.handle):
        user = auth.resolve_session(frontflow_session)
        require_submission_visibility(form, submission, user)
    return [
        CommentOut(**c)
        for c in store.list_comments(form_id, submission.handle, thread_id)
    ]


@api.post(
    "/forms/{form_id}/submissions/{submission_id}/comments/{thread_id}",
    response_model=CommentOut,
    dependencies=[Depends(require_form_visibility)],
)
def create_comment(
    form_id: str, submission_id: str, thread_id: str,
    payload: CommentIn,
    frontflow_session: str | None = Cookie(default=None),
    token: str | None = None,
) -> CommentOut:
    form, submission = _get_submission_or_404(form_id, submission_id)
    if not _token_bears_submission(token, submission.handle):
        user = auth.resolve_session(frontflow_session)
        require_submission_visibility(form, submission, user)
    else:
        user = auth.resolve_session(frontflow_session)
    author = (
        user.username
        if user is not None
        else (payload.author or "").strip() or "anonymous"
    )
    stored = store.add_comment(
        form_id,
        submission.handle,
        thread_id,
        author=author,
        body=payload.body,
        user_id=user.username if user is not None else None,
    )
    return CommentOut(**stored)


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
    """Set a form's visibility mode. Returns 409 Conflict when the
    form's DSL declares visibility (e.g. `@form(private=True)`) —
    the source is the source of truth; the admin UI cannot override
    settings declared in code."""
    try:
        auth.set_form_visibility(form_id, req.visibility)
    except auth.FormVisibilityLocked as e:
        raise HTTPException(status_code=409, detail=str(e))
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


# --- Variables -------------------------------------------------------------


class VariableSummary(BaseModel):
    """A variable's metadata — never includes the value. The value
    is write-only over the API: an admin enters it on create/update,
    and the server returns it only at template-resolution time."""
    name: str
    description: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class VariableInput(BaseModel):
    """Create/update payload. The value is write-only: supply it to
    set or rotate, omit it on an update to keep the stored one.
    Creating a variable with no value is an error."""
    value: Optional[str] = None
    description: Optional[str] = None


def _variable_summary(rec: dict) -> VariableSummary:
    return VariableSummary(
        name=rec["name"],
        description=rec.get("description"),
        created_at=rec["created_at"],
        updated_at=rec["updated_at"],
    )


@api.get(
    "/variables",
    response_model=list[VariableSummary],
    dependencies=[Depends(require_admin)],
)
def list_variables() -> list[VariableSummary]:
    """Every stored variable, metadata only."""
    return [_variable_summary(v) for v in store.list_variables()]


@api.get(
    "/variables/{name}",
    response_model=VariableSummary,
    dependencies=[Depends(require_admin)],
)
def read_variable(name: str) -> VariableSummary:
    """One variable's metadata. The value is never returned — the
    editor re-enters it to rotate, matching the connections pattern.
    Even an admin requesting their own variable does not receive the
    value over the API."""
    rec = store.get_variable_meta(name)
    if rec is None:
        raise HTTPException(
            status_code=404, detail=f"variable {name!r} not found"
        )
    return _variable_summary(rec)


@api.put(
    "/variables/{name}",
    response_model=VariableSummary,
    dependencies=[Depends(require_admin)],
)
def write_variable(name: str, body: VariableInput) -> VariableSummary:
    """Create or update a variable. Omitting the value on an update
    keeps the stored one; a new variable requires a non-empty value."""
    if not _VARIABLE_NAME_RE.match(name):
        raise HTTPException(
            status_code=422,
            detail=(
                "variable name must be 1-64 chars, alphanumerics + "
                "underscores, starting with a letter or underscore"
            ),
        )

    # An empty string is a meaningful value (clearing a config),
    # but distinct from "no value supplied" — Pydantic-default None
    # means "keep what's stored." A user that genuinely wants to
    # blank a variable supplies "".
    existing = store.get_variable_meta(name)
    if existing is None and body.value is None:
        raise HTTPException(
            status_code=422,
            detail="a new variable requires a value",
        )

    store.upsert_variable(
        name=name,
        value=body.value,
        description=body.description,
    )
    return _variable_summary(store.get_variable_meta(name))


@api.delete("/variables/{name}", dependencies=[Depends(require_admin)])
def remove_variable(name: str) -> dict[str, bool]:
    """Delete a variable. The variable disappears from the namespace
    immediately; any workflow file using it via the Python helper
    will fail to load on next reparse, surfacing in /api/forms's
    LOAD_ERRORS."""
    if not store.delete_variable(name):
        raise HTTPException(
            status_code=404, detail=f"variable {name!r} not found"
        )
    return {"deleted": True}


# Variable names — same shape as connection names: a Python-ish
# identifier (so the templating proxy's `.attr` syntax works) plus
# a length cap to keep the namespace browsable in the admin UI.
_VARIABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


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
    # The node submits via an @backend.branch.
    is_branch: bool = False


class GraphPage(BaseModel):
    """A `@page` — an outer container enclosing the node-groups whose
    nodes belong to it. Renders in the workflow graph as a labeled
    box wrapping its members, collapsible like an Airflow task_group.

    The backend always emits a page entry for every `@page` in the
    workflow, regardless of member count — the viewer decides whether
    to draw or collapse it. `member_group_ids` lists the GraphGroup
    ids that belong to this page, in declaration order."""
    id: str
    title: str
    member_group_ids: list[str]


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
    # Outer page containers (one per `@page`) — each entry lists the
    # GraphGroup ids that belong to it. Empty list when the workflow
    # has no `@page` declarations (everything is top-level @node).
    # Always emitted regardless of member count; the frontend decides
    # rendering (always draws the container, collapsible to a single
    # condensed node).
    pages: list[GraphPage] = Field(default_factory=list)
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

    def backend_node_id(node_id: str, fn_name: str) -> str:
        # Multi-backend per node — qualify by function name. (Older
        # single-backend graphs used `<node>#backend`; this is a
        # compatible-shaped id keyed by fn name to disambiguate.)
        return f"{node_id}#backend:{fn_name}"

    def add_node_group(cn: CompiledNode, page_id: Optional[str]) -> None:
        # A node branches if its chain contains a `@backend.branch`,
        # or ends in an HitlBranch operator.
        chain_branch_backend = next(
            (
                cs.backend_call for cs in cn.chain
                if cs.kind == "backend_call"
                and cs.backend_call.fn.is_branch
            ),
            None,
        )
        node_branches = chain_branch_backend is not None or any(
            t.kind == "airflow_hitl_branch" for t in cn.external_tasks
        )
        groups.append(
            GraphGroup(
                id=cn.id,
                title=cn.title,
                page_id=page_id,
                is_branch=node_branches,
            )
        )
        input_ids = {f.name for f in cn.fields}
        node_input_ids[cn.id] = input_ids
        # The first backend in the chain stays the "primary" for any
        # dependency-edge consumers that key off `node_backend_fn`.
        first_bc = next(
            (
                cs.backend_call for cs in cn.chain
                if cs.kind == "backend_call"
            ),
            None,
        )
        node_backend_fn[cn.id] = first_bc.fn.name if first_bc is not None else None

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

        # Chain steps — backends and operators in declared `>>` order.
        # Each gets a rank that continues past the inputs; consecutive
        # chain steps connect to each other. Backend args that
        # reference inputs draw in_group edges from those inputs.
        # Operator with `graph_visible=False` (legacy plumbing) is
        # skipped — same as before.
        prev_id: Optional[str] = None
        chain_rank = max_input_rank + 1
        for cs in cn.chain:
            if cs.kind == "backend_call":
                bc = cs.backend_call
                gid = backend_node_id(cn.id, bc.fn.name)
                nodes.append(
                    GraphNode(
                        id=gid,
                        kind="backend",
                        group_id=cn.id,
                        label=bc.fn.name,
                        detail="branch" if bc.fn.is_branch else None,
                        rank=chain_rank,
                        is_branch=bc.fn.is_branch,
                    )
                )
                # Input-arg edges → this backend.
                for arg in bc.arg_op_ids:
                    if arg in input_ids:
                        edges.append(
                            GraphEdge(
                                id=f"ig:{cn.id}:{arg}->{bc.fn.name}",
                                from_node=f"{cn.id}::{arg}",
                                to_node=gid,
                                relation="in_group",
                            )
                        )
                if prev_id is not None:
                    edges.append(
                        GraphEdge(
                            id=f"ig:{cn.id}:{prev_id}->{gid}",
                            from_node=prev_id,
                            to_node=gid,
                            relation="in_group",
                        )
                    )
                prev_id = gid
                chain_rank += 1
            elif cs.kind == "external_task":
                ext = cs.external_task
                if not ext.graph_visible:
                    continue  # hidden plumbing — skip
                gid = f"{cn.id}::{ext.task_id}"
                nodes.append(
                    GraphNode(
                        id=gid,
                        kind="airflow",
                        group_id=cn.id,
                        label=ext.task_id,
                        detail=ext.kind,
                        rank=chain_rank,
                    )
                )
                if prev_id is not None:
                    edges.append(
                        GraphEdge(
                            id=f"ig:{cn.id}:{prev_id}->{gid}",
                            from_node=prev_id,
                            to_node=gid,
                            relation="in_group",
                        )
                    )
                prev_id = gid
                chain_rank += 1

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

    # Per-node lookup: every backend fn name in that node's chain,
    # used to route `steps.<node>.<fn_name>` references to the right
    # backend graph node.
    node_all_backend_fns: dict[str, set[str]] = {}
    for cn in form.all_nodes_by_id.values():
        node_all_backend_fns[cn.id] = {
            cs.backend_call.fn.name
            for cs in cn.chain
            if cs.kind == "backend_call"
        }

    def resolve_endpoint(node_id: str, field: Optional[str]) -> Optional[str]:
        # A whole-node reference resolves to the node's *primary*
        # backend — the first one in the chain — or to the group when
        # the node has no backend. Mirrors the legacy semantics.
        if field is None:
            if node_backend_fn.get(node_id):
                return backend_node_id(
                    node_id, node_backend_fn[node_id],
                )
            return node_id if node_id in node_input_ids else None
        if field in node_input_ids.get(node_id, set()):
            return f"{node_id}::{field}"
        if field in node_all_backend_fns.get(node_id, set()):
            return backend_node_id(node_id, field)
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

    # Collect page-level containers — one entry per `@page`, listing
    # the GraphGroup ids belonging to it in declaration order. The
    # frontend always draws these as outer containers (with a
    # collapse toggle); the backend emits them unconditionally
    # regardless of member count.
    pages: list[GraphPage] = []
    for step in form.steps:
        if isinstance(step, CompiledPage):
            pages.append(
                GraphPage(
                    id=step.id,
                    title=step.title or step.id,
                    member_group_ids=[sn.id for sn in step.nodes],
                )
            )

    return WorkflowGraph(
        form_id=form.id,
        title=form.title,
        groups=groups,
        pages=pages,
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


class FormSourceResponse(BaseModel):
    """Raw Python source for a form. The frontend renders this
    read-only with syntax highlighting on a Source tab. `version`
    is the human-facing major integer the source was compiled from;
    `minor_version` is the non-structural revision within that
    major. Rendered together as `v{version}.{minor_version}`,
    suppressing the suffix when minor=0."""
    form_id: str
    version: int
    minor_version: int = 0
    source: str


@api.get(
    "/forms/{form_id}/source",
    response_model=FormSourceResponse,
    dependencies=[Depends(require_admin)],
)
def read_form_source(form_id: str) -> FormSourceResponse:
    """The form's current source — the .py file the live workflow
    was compiled from. Admin-only since source files can carry
    sensitive details (e.g. example secrets, API keys in comments).

    Read from the latest persisted form_version rather than the
    in-memory `form_meta` so the response always matches what the
    runtime would compile if reloaded — the two stay in sync but
    the DB is the canonical store.
    """
    if form_id not in FORMS:
        raise HTTPException(status_code=404, detail="form not found")
    version_id = FORM_VERSION_IDS.get(form_id)
    if version_id is None:
        # In-memory form without a persisted version — the DB upsert
        # raised at scan time (commonly because the existing table
        # still carries an old unique constraint that the migration
        # hasn't been able to drop yet). Serve the source we kept in
        # memory so the user at least sees current code while the
        # underlying issue is investigated.
        folder, source = FORM_SOURCES.get(form_id, ("", ""))
        return FormSourceResponse(
            form_id=form_id, version=0, minor_version=0, source=source,
        )
    fv = store.get_form_version(version_id)
    if fv is None:
        # Shouldn't happen — the id came from a successful upsert —
        # but defend against a manual DB edit.
        raise HTTPException(
            status_code=500,
            detail="live form_version row missing from store",
        )
    return FormSourceResponse(
        form_id=form_id,
        version=fv["version"],
        minor_version=fv["minor_version"],
        source=fv["source"],
    )


@api.get(
    "/forms/{form_id}/versions/{version}/source",
    response_model=FormSourceResponse,
    dependencies=[Depends(require_admin)],
)
def read_form_version_source(
    form_id: str, version: int,
) -> FormSourceResponse:
    """The pinned source for a specific form version. Surfaces what
    a submission was actually running, even after the live form
    has been edited and bumped past it. Returns the LATEST minor
    of the requested major — callers wanting a specific minor must
    look it up by form_version_id, not by integer version."""
    fv = store.get_form_version_by_number(form_id, version)
    if fv is None:
        raise HTTPException(
            status_code=404,
            detail=f"form version {version} not found for {form_id!r}",
        )
    return FormSourceResponse(
        form_id=form_id,
        version=fv["version"],
        minor_version=fv["minor_version"],
        source=fv["source"],
    )


class DiffLine(BaseModel):
    """One line of a unified diff. `kind` is one of:
      - "context"   — unchanged line, shown for orientation
      - "add"       — present only in the `to` source
      - "remove"    — present only in the `from` source
    `text` carries the raw line WITHOUT the leading +/-/space marker
    (the frontend renders its own gutter). `from_lineno` / `to_lineno`
    are 1-based source line numbers, omitted for the side a line
    doesn't exist on (e.g. an add has only `to_lineno`)."""
    kind: str
    text: str
    from_lineno: int | None = None
    to_lineno: int | None = None


class DiffHunk(BaseModel):
    """One contiguous block of changed-or-context lines. A diff is a
    sequence of hunks separated by un-shown identical regions; this
    matches the unified-diff output `difflib.unified_diff` produces.
    `header` is the textual `@@ -a,b +c,d @@` line so the UI can show
    it; the parsed range fields are there for callers that prefer
    the numbers without re-parsing."""
    header: str
    from_start: int
    from_count: int
    to_start: int
    to_count: int
    lines: list[DiffLine]


class FormVersionDiffSide(BaseModel):
    """Lightweight metadata for one side of a diff comparison. The
    `source` itself is not duplicated here — the line-by-line content
    is reconstructable from the hunks (for what's shown) or already
    fetchable via /forms/{id}/versions/{n}/source for the full text."""
    form_version_id: int
    version: int
    minor_version: int


class FormVersionDiffResponse(BaseModel):
    """Unified diff between two form versions of the same form.

    `bump` summarizes the relationship between `from_version` and
    `to_version` AS COMPARED — "major" if the majors differ, "minor"
    if only the minors differ, "none" if they're literally the same
    row (then `hunks` is empty). Independent of which direction the
    user picked: comparing v3 → v2 still classifies as "major"."""
    from_version: FormVersionDiffSide
    to_version: FormVersionDiffSide
    bump: str  # "none" | "minor" | "major"
    added_lines: int
    removed_lines: int
    hunks: list[DiffHunk]


def _parse_unified_hunks(diff_lines: list[str]) -> list[DiffHunk]:
    """Parse the raw unified-diff output from `difflib.unified_diff`
    into structured hunks. The first two lines are the file headers
    (`--- a` / `+++ b`); after that each hunk starts with `@@` and
    contains marker-prefixed body lines (` `, `+`, `-`).

    Why server-side parsing rather than shipping the raw text:
    - The frontend wants per-line line numbers for the gutter; those
      have to be tracked anyway, and doing it once here avoids every
      consumer re-implementing it.
    - The diff line-count summary (`added_lines`, `removed_lines`) is
      the same walk; we can fold it in.
    - The frontend then becomes "render this structured data" — no
      string parsing in TypeScript.
    """
    import re

    # Hunk-header regex. `count` is optional in unified-diff format
    # when the range is exactly one line — `@@ -5 +5,3 @@` is valid.
    header_re = re.compile(
        r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@"
    )

    hunks: list[DiffHunk] = []
    # Skip the first two `---` / `+++` file-header lines if present.
    # `difflib.unified_diff` emits them when called with the default
    # `n=3` context; we don't carry them through.
    i = 0
    while i < len(diff_lines) and not diff_lines[i].startswith("@@"):
        i += 1

    while i < len(diff_lines):
        line = diff_lines[i]
        m = header_re.match(line)
        if not m:
            # Defensive — should always start with @@ when we're here.
            i += 1
            continue
        from_start = int(m.group(1))
        from_count = int(m.group(2)) if m.group(2) is not None else 1
        to_start = int(m.group(3))
        to_count = int(m.group(4)) if m.group(4) is not None else 1
        hunk_lines: list[DiffLine] = []
        from_lineno = from_start
        to_lineno = to_start
        i += 1
        while i < len(diff_lines) and not diff_lines[i].startswith("@@"):
            body = diff_lines[i]
            # difflib produces trailing newlines on each yielded
            # line; strip the line terminator so the UI doesn't get
            # a phantom blank row.
            body = body.rstrip("\n")
            if body.startswith("+") and not body.startswith("+++"):
                hunk_lines.append(DiffLine(
                    kind="add", text=body[1:], to_lineno=to_lineno,
                ))
                to_lineno += 1
            elif body.startswith("-") and not body.startswith("---"):
                hunk_lines.append(DiffLine(
                    kind="remove", text=body[1:], from_lineno=from_lineno,
                ))
                from_lineno += 1
            elif body.startswith(" "):
                hunk_lines.append(DiffLine(
                    kind="context", text=body[1:],
                    from_lineno=from_lineno, to_lineno=to_lineno,
                ))
                from_lineno += 1
                to_lineno += 1
            elif body == "":
                # Bare empty line inside a hunk — treat as a context
                # blank, advancing both line counters.
                hunk_lines.append(DiffLine(
                    kind="context", text="",
                    from_lineno=from_lineno, to_lineno=to_lineno,
                ))
                from_lineno += 1
                to_lineno += 1
            # else: defensive — `\ No newline at end of file` etc.;
            # ignored.
            i += 1
        hunks.append(DiffHunk(
            header=line.rstrip("\n"),
            from_start=from_start, from_count=from_count,
            to_start=to_start, to_count=to_count,
            lines=hunk_lines,
        ))
    return hunks


@api.get(
    "/forms/{form_id}/versions/{from_id}/diff/{to_id}",
    response_model=FormVersionDiffResponse,
    dependencies=[Depends(require_admin)],
)
def diff_form_versions(
    form_id: str, from_id: int, to_id: int,
) -> FormVersionDiffResponse:
    """Unified diff between two form versions identified by row id.

    Both ids must belong to `form_id` — cross-form comparisons are
    rejected (404) so the URL can't be abused to peek at sources from
    a form the requester doesn't own. Admin-only.

    The diff is computed with `difflib.unified_diff` (context = 3),
    then parsed into structured hunks the UI can render directly —
    line numbers per side, +/- gutter markers, line-count summary.
    Same-version comparisons return an empty hunk list with `bump =
    "none"` rather than 404'ing, so the UI can render an "(identical)"
    state when the admin picks the same version on both sides.
    """
    fv_from = store.get_form_version(from_id)
    if fv_from is None or fv_from["form_id"] != form_id:
        raise HTTPException(
            status_code=404,
            detail=f"form version {from_id} not found for {form_id!r}",
        )
    fv_to = store.get_form_version(to_id)
    if fv_to is None or fv_to["form_id"] != form_id:
        raise HTTPException(
            status_code=404,
            detail=f"form version {to_id} not found for {form_id!r}",
        )

    # Major-vs-minor-vs-none classification compares the two SIDES,
    # not from→to direction — comparing v3 → v2 is still "major".
    if fv_from["id"] == fv_to["id"]:
        bump = "none"
    elif fv_from["version"] != fv_to["version"]:
        bump = "major"
    else:
        bump = "minor"

    # `splitlines(keepends=True)` preserves trailing newlines so
    # `difflib` can detect "no newline at end" correctly; the parser
    # strips them when building DiffLine.text for the wire.
    from difflib import unified_diff
    from_lines = fv_from["source"].splitlines(keepends=True)
    to_lines = fv_to["source"].splitlines(keepends=True)
    raw = list(unified_diff(
        from_lines, to_lines,
        fromfile=f"v{fv_from['version']}.{fv_from['minor_version']}",
        tofile=f"v{fv_to['version']}.{fv_to['minor_version']}",
        n=3,
    ))
    hunks = _parse_unified_hunks(raw)

    # Totals — used in the modal header summary so the admin sees the
    # diff's scale before scrolling. Counts here mirror what `git
    # diff --stat` would report: a single line modified counts as
    # one add AND one remove.
    added = sum(
        1 for h in hunks for l in h.lines if l.kind == "add"
    )
    removed = sum(
        1 for h in hunks for l in h.lines if l.kind == "remove"
    )

    return FormVersionDiffResponse(
        from_version=FormVersionDiffSide(
            form_version_id=fv_from["id"],
            version=fv_from["version"],
            minor_version=fv_from["minor_version"],
        ),
        to_version=FormVersionDiffSide(
            form_version_id=fv_to["id"],
            version=fv_to["version"],
            minor_version=fv_to["minor_version"],
        ),
        bump=bump,
        added_lines=added,
        removed_lines=removed,
        hunks=hunks,
    )

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
    form_id: str, req: StartSubmissionRequest,
    frontflow_session: str | None = Cookie(default=None),
) -> SubmissionResponse:
    form = _get_form_or_404(form_id)
    _validate_landing_values(form, req.values)
    # Drop values for fields hidden by an unsatisfied `When`. The
    # landing node has no upstream, so its conditions are all same-node.
    values = _prune_inactive(
        form.landing_node().fields, dict(req.values), {}
    )

    # Resolve acting user from the cookie session BEFORE invoking
    # the runtime — _execute_assigns runs during start_submission
    # (for landing-node Assigns) and reads submission.user_id for
    # the assignment's granted_by_user_id. Without this, the
    # landing-node Assign was silently skipped.
    acting_user = auth.resolve_session(frontflow_session)
    acting_user_id = (
        acting_user.id if acting_user is not None else None
    )

    try:
        submission = start_submission(
            form,
            initial_form_values=values,
            form_version_id=FORM_VERSION_IDS.get(form_id, 0),
            acting_user_id=acting_user_id,
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


class CurrentStepOption(BaseModel):
    """One entry in the "current step" filter dropdown — node id +
    count of submissions currently at that step. The UI shows the
    count next to the option so the filter target is discoverable
    ("Review (42)") rather than a bare node id list."""
    node_id: str
    count: int


@api.get(
    "/forms/{form_id}/submissions/current-steps",
    response_model=list[CurrentStepOption],
)
def read_form_submission_current_steps(
    form_id: str,
    user: "store.User" = Depends(_current_user),
    show_deleted: bool = Query(default=False),
) -> list[CurrentStepOption]:
    """The distinct `current_step` values across this form's
    submissions plus the count at each — backing the "current
    step" filter dropdown.

    Visibility model matches the listing endpoint: non-admin /
    non-folder-grant users see only the steps their visible
    submissions are currently on. Counts are scoped to their
    visible set too — so the dropdown labels stay truthful
    ("Review (3)" really means three rows the user can see).

    `show_deleted=1` admin-only — same gate as the listing's flag.

    *Route ordering note*: this declaration is intentionally placed
    BEFORE `/forms/{form_id}/submissions/{submission_id}`. FastAPI
    matches routes in declaration order, and `current-steps` is a
    valid `{submission_id}` match — placing this first ensures the
    literal segment wins. Moving this declaration below the detail
    route would silently break the dropdown with a 404 from the
    detail handler.
    """
    if not store.form_exists(form_id):
        raise HTTPException(
            status_code=404, detail=f"form {form_id!r} not found"
        )
    include_deleted = bool(show_deleted) and bool(
        getattr(user, "is_admin", False)
    )
    rows = store.list_form_submission_current_steps(
        form_id, include_deleted=include_deleted,
    )
    # Visibility filter: for non-full-access users, recompute the
    # counts after intersecting with their visible handles. The
    # store function doesn't take a handle whitelist (its query is
    # a group-by, not row-by-row), so we filter here at the cost of
    # one extra count. Admins and folder-grants short-circuit.
    visible_handles = _visible_submission_handles_for_user(form_id, user)
    if visible_handles is None:
        return [CurrentStepOption(**r) for r in rows]
    if not visible_handles:
        return []
    # Per-step recount over the user's visible subset. Cheap; we're
    # querying the same window-ranked subquery the store function
    # used, so this is at most one round-trip per dropdown open.
    page = store.list_form_submissions(
        form_id,
        limit=store._LISTING_MAX_LIMIT,
        offset=0,
        handle_whitelist=visible_handles,
        include_deleted=include_deleted,
    )
    counts: dict[str, int] = {}
    for s in page["submissions"]:
        if s["current_step"]:
            counts[s["current_step"]] = counts.get(
                s["current_step"], 0,
            ) + 1
    return sorted(
        (
            CurrentStepOption(node_id=k, count=v)
            for k, v in counts.items()
        ),
        key=lambda o: (-o.count, o.node_id),
    )


@api.get(
    "/forms/{form_id}/submissions/{submission_id}",
    response_model=SubmissionResponse,
    dependencies=[Depends(require_form_visibility)],
)
def read_submission(
    form_id: str, submission_id: str,
    frontflow_session: str | None = Cookie(default=None),
    token: str | None = None,
) -> SubmissionResponse:
    form, submission = _get_submission_or_404(form_id, submission_id)
    # Submission-level visibility: form-level says "may you reach
    # the form's URLs"; submission-level says "may you see THIS
    # submission". A valid signed-link token bypasses (the bearer
    # is the intended viewer of this specific submission).
    if not _token_bears_submission(token, submission.handle):
        user = auth.resolve_session(frontflow_session)
        require_submission_visibility(form, submission, user)
    # Source-unavailable: skip advance/persist (placeholders would
    # raise). Read endpoints stay viewable; no forward progress is
    # attempted. Mirrors the detail-endpoint handling.
    if not getattr(form, "source_unavailable", False):
        advance(form, submission)
        _persist(form, submission)
    return _build_submission_response(submission, form)


@api.get(
    "/forms/{form_id}/submissions/{submission_id}/steps/{step_id}",
    response_model=StepDetail,
)
def read_step(
    form_id: str, submission_id: str, step_id: str,
    frontflow_session: str | None = Cookie(default=None),
    key: str | None = None,
    token: str | None = None,
) -> StepDetail:
    form, submission = _get_submission_or_404(form_id, submission_id)
    # Token-aware visibility check. Without this, restricted forms
    # would 404 a signed-link-bearing visitor before the role
    # check ever ran.
    _check_form_visibility_with_token(
        form_id, submission.handle, frontflow_session, key, token,
    )
    # Submission-level visibility — even on a public form, only
    # the submission's contributors/assignees/granters/admins/folder-
    # grant-holders can read its steps. Bypassed by signed-link
    # token. (Phase 4.5 role check applies AFTER this, deciding
    # what fields the caller can read/write WITHIN the visible
    # submission.)
    if not _token_bears_submission(token, submission.handle):
        user = auth.resolve_session(frontflow_session)
        require_submission_visibility(form, submission, user)
    if not getattr(form, "source_unavailable", False):
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

    # Runtime auth check — decide what THIS user can read/write on
    # THIS node, given their assignments on THIS submission and the
    # form's permission template snapshot. The verdict is surfaced
    # on the response; the frontend renders pending state when the
    # user is gated out, and disables inputs they can't write.
    from frontflow.dsl.permissions import resolve_node_access
    user_roles, is_admin, _ = _resolve_user_roles_on_submission(
        submission.handle, frontflow_session, token=token,
    )
    node_access = resolve_node_access(
        node_role=getattr(ng, "role", None),
        default_role_mode=(
            form.permission_template.get("default_role_mode", "open")
        ),
        user_roles_on_submission=user_roles,
        is_admin=is_admin,
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

    # Build the access dict the frontend will consume.
    access_payload: dict[str, Any] = {
        "can_read": node_access.can_read,
        "can_write": node_access.can_write,
        "pending": node_access.pending,
    }
    if node_access.missing_write_roles:
        access_payload["missing_write_roles"] = list(
            node_access.missing_write_roles
        )

    # When pending=True the user has no access to this node's
    # contents — strip the layout to a minimal placeholder so we
    # don't leak field labels / values / help text. Read-only
    # (can_read=True, can_write=False) keeps the full layout
    # because the user IS permitted to see the state; only
    # interaction is disabled.
    if node_access.pending:
        layout_block = _to_block(_make_pending_placeholder(
            ng, node_access.missing_write_roles,
        ))
        response_payload = None
        draft_payload = None
    else:
        layout_block = _to_block(
            resolve_layout(form, submission, ng.layout, step_id)
        )

    return StepDetail(
        handle=submission.handle,
        submission_id=submission.submission_id,
        step_id=step_id,
        layout=layout_block,
        response_received=step.is_submitted,
        response=response_payload,
        draft=draft_payload,
        status=step.status.key,
        edit_in_progress=submission.editing_node_id == step_id,
        responded_at=(
            step.submitted_at.isoformat() if step.submitted_at else None
        ),
        page_id=page.id if page is not None else None,
        page_title=page.title if page is not None else None,
        error=step.error,
        access=access_payload,
    )


def _make_pending_placeholder(
    ng: Any, missing_roles: tuple[str, ...],
) -> Any:
    """Build a layout placeholder for a node the user is gated out
    of. Returns a CompiledBlock (markdown) telling the user the
    node is assigned to a role they don't hold. Avoids leaking
    the actual layout's labels, options, or help text.
    """
    from frontflow.dsl.compile import CompiledBlock
    roles_str = ", ".join(missing_roles) if missing_roles else "another role"
    text = (
        f"### Waiting on someone else\n\n"
        f"This step is assigned to **{roles_str}**. You'll be "
        f"able to act on it when your colleague completes their "
        f"part."
    )
    return CompiledBlock(
        type="markdown",
        id="_pending_placeholder",
        props={"text": text},
        children=[],
    )


@api.get(
    "/forms/{form_id}/submissions/{submission_id}/download/{node_id}/{block_id}",
    dependencies=[Depends(require_form_visibility)],
)
def download_s3(
    form_id: str, submission_id: str, node_id: str, block_id: str,
) -> RedirectResponse:
    """Proxy for an `displays.S3Download` block.

    Resolves the block in the named node's layout, templates the
    `key` against the current submission's `steps` namespace, asks
    `S3Hook` to mint a fresh presigned URL, and 302-redirects the
    browser to S3. Generating the URL only at click time means the
    download link survives indefinitely — the user can keep the form
    page open as long as they like and the link will still work when
    they finally click.

    Visibility is enforced by `require_form_visibility`; only users
    who can see the submission can pull files it surfaces.
    """
    form, submission = _get_submission_or_404(form_id, submission_id)
    # Advance the submission so any in-flight chain settles before we
    # look at the layout (same pattern as `read_step`). Skip for
    # source-unavailable submissions — backends are inert placeholders.
    # The recorded download links still resolve from layout props.
    if not getattr(form, "source_unavailable", False):
        advance(form, submission)

    ng = form.all_nodes_by_id.get(node_id)
    if ng is None:
        raise HTTPException(
            status_code=404, detail=f"node {node_id!r} not found"
        )

    # Resolve the layout against the submission's data; the resolver
    # templates props (including the `key`) using the same nested
    # `steps` namespace the rest of the system uses.
    resolved = resolve_layout(form, submission, ng.layout, node_id)

    block = _find_s3_download_block(resolved, block_id)
    if block is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"s3_download block {block_id!r} not found in node "
                f"{node_id!r}"
            ),
        )

    bucket = block.props.get("bucket")
    key = block.props.get("key")
    if not bucket or not isinstance(key, str) or "{{" in key:
        # `{{` left in the key means a template token couldn't resolve
        # (e.g. the upstream node hasn't run yet). Surface that clearly
        # rather than passing a half-baked key to S3.
        raise HTTPException(
            status_code=409,
            detail=(
                "the download isn't ready — the S3 key references "
                "an upstream value that hasn't been submitted yet"
            ),
        )

    # Lazy import — keep S3Hook (and boto3) out of the import path of
    # workflows that don't use S3 at all.
    from frontflow.aws.hooks import S3Hook

    connection = block.props.get("connection")
    expires_in = int(block.props.get("expires_in") or 300)
    # `filename` is a TEMPLATED_PROPS entry, so by the time we see it
    # on the resolved block it has any `{{ steps.<x> }}` references
    # substituted in. None or empty means "let the browser fall back
    # to the key's basename" — we don't pass a Content-Disposition in
    # that case.
    filename = block.props.get("filename")
    if isinstance(filename, str):
        filename = filename.strip()
    content_disposition = (
        _build_content_disposition(filename) if filename else None
    )
    try:
        url = S3Hook(connection_name=connection).presigned_get_url(
            bucket=bucket, key=key, expires_in=expires_in,
            response_content_disposition=content_disposition,
        )
    except ImportError as e:
        # boto3 missing — server isn't set up to serve S3 downloads.
        raise HTTPException(status_code=500, detail=str(e))
    return RedirectResponse(url=url, status_code=307)


def _build_content_disposition(filename: str) -> str:
    """Build an HTTP `Content-Disposition: attachment` header value
    that names `filename` as the download's saved name.

    Emits BOTH the legacy `filename="..."` parameter (ASCII-only,
    quotes escaped) AND the RFC 5987 `filename*=UTF-8''<percent-
    encoded>` parameter. Browsers prefer `filename*` when present
    and fall back to `filename` otherwise; emitting both gives
    correct behavior for ASCII clients on quirky middleware AND
    non-ASCII filenames everywhere.

    Both parameters are needed because:
      - The legacy `filename="..."` works in every browser but
        gets mangled for non-ASCII characters (which is most
        templated values that come from user-supplied data).
      - `filename*` is the standards-compliant Unicode variant
        but isn't recognized by some non-browser HTTP clients.
    """
    from urllib.parse import quote
    # Drop control characters that would break header serialization.
    safe = "".join(c for c in filename if c.isprintable() or c == " ")
    # ASCII-only fallback: replace anything non-ASCII with `_`, then
    # backslash-escape `"` and `\` so the quoted-string is legal.
    ascii_only = (
        safe.encode("ascii", "replace")
        .decode("ascii")
        .replace("?", "_")
    )
    ascii_quoted = ascii_only.replace("\\", "\\\\").replace('"', '\\"')
    encoded = quote(safe, safe="")
    return (
        f'attachment; filename="{ascii_quoted}"; '
        f"filename*=UTF-8''{encoded}"
    )


def _find_s3_download_block(
    block: "CompiledBlock", block_id: str
) -> "Optional[CompiledBlock]":
    """Walk a resolved layout tree, returning the first `s3_download`
    block whose id matches. Used by the download proxy endpoint."""
    if block.type == "s3_download" and block.id == block_id:
        return block
    for child in block.children:
        hit = _find_s3_download_block(child, block_id)
        if hit is not None:
            return hit
    return None


@api.get(
    "/forms/{form_id}/submissions/{submission_id}/blob/{blob_hash}",
    dependencies=[Depends(require_form_visibility)],
)
def get_blob(
    form_id: str, submission_id: str, blob_hash: str,
) -> Response:
    """Stream a content-addressed blob a `@backend` produced (e.g. a
    matplotlib figure rendered to PNG bytes). Authorized via the
    submission's visibility rules — only users who can see the
    submission can read its blobs.

    The blob's stored `Content-Type` flows through unchanged so the
    browser renders the image in-place via `<img>`."""
    # Resolve the submission so we have its handle for the blob lookup.
    # `form_id` validates via require_form_visibility; the submission
    # lookup additionally checks it belongs to that form.
    _form, submission = _get_submission_or_404(form_id, submission_id)
    result = store.get_submission_blob(
        submission_handle=submission.handle, blob_hash=blob_hash,
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"blob {blob_hash!r} not found in this submission",
        )
    body, content_type = result
    return Response(
        content=body, media_type=content_type,
        headers={
            # The hash never changes, so the response is safe to
            # cache aggressively. The user's session cookie gates
            # access, not the URL.
            "Cache-Control": "private, max-age=86400, immutable",
        },
    )


@api.post(
    "/forms/{form_id}/submissions/{submission_id}/steps/{step_id}",
    response_model=StepDetail,
)
def submit_step_endpoint(
    form_id: str,
    submission_id: str,
    step_id: str,
    req: StepSubmissionRequest,
    frontflow_session: str | None = Cookie(default=None),
    key: str | None = None,
    token: str | None = None,
) -> StepDetail:
    form, submission = _get_submission_or_404(form_id, submission_id)
    _check_form_visibility_with_token(
        form_id, submission.handle, frontflow_session, key, token,
    )
    _require_source_available(form)
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

    # Runtime auth check: a non-admin user without write access on
    # this node cannot submit it. The check matches what read_step
    # surfaces — pending nodes 403, read-only nodes 403, fully
    # gated-out 403. Admins always pass. Open-mode forms (no role
    # declared) always pass.
    from frontflow.dsl.permissions import (
        resolve_field_access, resolve_node_access,
    )
    user_roles, is_admin, acting_user_id = (
        _resolve_user_roles_on_submission(
            submission.handle, frontflow_session, token=token,
        )
    )
    node_access = resolve_node_access(
        node_role=getattr(ng, "role", None),
        default_role_mode=(
            form.permission_template.get("default_role_mode", "open")
        ),
        user_roles_on_submission=user_roles,
        is_admin=is_admin,
    )
    if not node_access.can_write:
        # Distinguish "you can read but not write" (e.g., monitor
        # role with view-only access) from "you have no access at
        # all" — both block the submit but the message differs.
        if node_access.can_read:
            detail = (
                "You don't have write access to this step — your "
                "role is read-only here."
            )
        else:
            missing = ", ".join(node_access.missing_write_roles) or "another role"
            detail = (
                f"This step is assigned to {missing}. You can't "
                "submit it."
            )
        raise HTTPException(status_code=403, detail=detail)

    # Per-input write gates — if a field carries its own role= and
    # the user doesn't hold it, drop the user's submitted value for
    # that field. The runtime sees only fields the user was
    # permitted to write; the existing values (if any) are
    # preserved as they were. This matches the design: per-input
    # role= narrows write, never broadens it.
    submitted_values = dict(req.values)
    for f in ng.fields:
        if not f.role:
            continue
        fa = resolve_field_access(
            field_role=f.role, node_access=node_access,
            user_roles_on_submission=user_roles, is_admin=is_admin,
        )
        if not fa.can_write and f.name in submitted_values:
            del submitted_values[f.name]

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
        _validate_required_fields(ng, submitted_values, steps_data)
        pruned = _prune_inactive(
            ng.fields, submitted_values, steps_data
        )
        # Stamp the acting user on the submission so _execute_assigns
        # (and anything else that needs it) sees it. The mutation
        # is per-request; the runtime re-resolves on every submit.
        if acting_user_id is not None:
            submission.user_id = acting_user_id
        submit_step(
            form,
            submission,
            node_id=step_id,
            form_values=pruned,
            button_clicked=button_clicked,
            acting_user_id=acting_user_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # Tie any file uploads submitted on this step to the submission.
    _attach_upload_blobs(form, pruned, submission.handle)

    return read_step(
        form_id, submission_id, step_id, frontflow_session,
        key=key, token=token,
    )


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
    _require_source_available(form)
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
    # Clearing runs backends to re-resolve the chain after the reset.
    # On a source-unavailable submission, those backends are inert
    # placeholders — clear can't do its job. Refuse with the same
    # 409 the other mutating endpoints use; recovery is via repin.
    _require_source_available(form)
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

    # The landing step's submitted values may feed the submission-id
    # template — and the URL the user is on bakes in that id. Editing
    # the landing then would change the id, breaking the URL. So:
    # reject an individual landing-step edit only when the submission
    # id actually depends on the landing step. With no template (id
    # is a random handle) or a template referencing other steps, the
    # edit is safe.
    if (
        req.from_task_id is not None
        and req.from_task_id == form.landing_node().id
    ):
        template = form.submission_id_template
        if template:
            from frontflow.dsl.runtime import _STEPS_REF_RE
            refs = set(_STEPS_REF_RE.findall(template))
            if req.from_task_id in refs:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"step {req.from_task_id!r} is the entry step and "
                        "feeds this submission's id template — editing it "
                        "would change the id baked into the URL. Reset "
                        "the whole submission instead (omit from_task_id)."
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


class RepinIssue(BaseModel):
    kind: str
    node_id: str
    field: Optional[str] = None
    button: Optional[str] = None
    detail: str


class RepinResponse(BaseModel):
    repinned: bool
    from_version: int
    to_version: int
    # On a refused re-pin (`repinned: False`) the endpoint also returns
    # 409 and the issues list — the body still parses as this model so
    # the client can render the diff.
    issues: list[RepinIssue] = []
    # When `force=true` produces a selective truncation, these list the
    # node ids that survived on the active chain and the node ids that
    # were dropped (moved to read-only history). Empty when the repin
    # didn't truncate anything — either it was a clean repin or
    # nothing was force-dropped. Wires into the client so the user can
    # see "we kept v, dropped w" instead of guessing what happened.
    kept_steps: list[str] = []
    dropped_steps: list[str] = []


@api.post(
    "/forms/{form_id}/submissions/{submission_id}/repin",
    response_model=RepinResponse,
    dependencies=[Depends(require_admin)],
)
def repin_submission_endpoint(
    form_id: str, submission_id: str, force: bool = False,
) -> RepinResponse:
    """Re-pin a submission to the current (live) form version.

    Two modes:
      - `force=False` (default): validates shape compatibility. Returns
        409 with the diff if any submitted step is incompatible; on
        success, reuses the existing chain at the new version (today's
        behavior — useful for backward-compatible form changes).
      - `force=True`: keeps the longest valid prefix of the active
        chain; truncates from the first invalidated step onward. The
        dropped steps freeze as read-only history under the prior
        form_version_id and remain viewable via the version picker.
        The submission resumes in-flight at the first dropped step
        (or the live form's landing node if that step doesn't exist
        on the new version). Frozen Airflow runs / S3 uploads are
        NOT rewound — they live on as side effects of the historical
        chain.

    Admin-only — this mutates a submission's version pin, an action
    its original submitter did not initiate.
    """
    # `_get_submission_or_404` returns the workflow at the submission's
    # *pinned* version. For re-pin we need both: that pinned workflow
    # (as `current`) and the live one (as `live`).
    _, submission = _get_submission_or_404(form_id, submission_id)
    live_version = FORM_VERSION_IDS.get(form_id)
    if live_version is None:
        raise HTTPException(
            status_code=404, detail=f"form {form_id!r} not found"
        )
    if submission.form_version_id == live_version:
        # No newer version to re-pin to. The action should not have
        # been offered, but treat the no-op idempotently.
        return RepinResponse(
            repinned=False,
            from_version=submission.form_version_id,
            to_version=live_version,
            issues=[],
        )

    live = FORMS[form_id]
    from_version = submission.form_version_id

    if force:
        # Selective force-repin: keep the longest valid prefix on the
        # active chain, freeze the rest as read-only history under the
        # prior form_version_id. Needs `current` (the old workflow) to
        # run validate_repin against, same as the non-force path.
        # Persist *first* so the v(old) chain rows are in the DB before
        # we bump the pin — after the bump, sync_submission scopes to
        # v(new) and would not write them.
        try:
            current = resolve_workflow(submission.form_version_id)
        except KeyError:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"submission's current form_version "
                    f"{submission.form_version_id} is not in the version "
                    "store — can't compute which steps to keep"
                ),
            )
        _persist(live, submission)
        summary = selective_force_repin_submission(
            current, live, submission, new_version_id=live_version,
        )
        _persist(live, submission)
        return RepinResponse(
            repinned=True,
            from_version=from_version,
            to_version=live_version,
            issues=[],
            kept_steps=summary["kept"],
            dropped_steps=summary["dropped"],
        )

    try:
        current = resolve_workflow(submission.form_version_id)
    except KeyError:
        raise HTTPException(
            status_code=409,
            detail=(
                f"submission's current form_version "
                f"{submission.form_version_id} is not in the version "
                "store — re-pinning would have nothing to diff against"
            ),
        )

    issues = repin_submission(
        current, live, submission, new_version_id=live_version
    )
    if issues:
        return JSONResponse(
            status_code=409,
            content=RepinResponse(
                repinned=False,
                from_version=from_version,
                to_version=live_version,
                issues=[RepinIssue(**i) for i in issues],
            ).model_dump(),
        )

    _persist(live, submission)
    return RepinResponse(
        repinned=True,
        from_version=from_version,
        to_version=live_version,
        issues=[],
    )


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
    # The submission_id whose event produced `last_activity`. Null
    # when the form has no events yet. Lets the form-summary "Last
    # activity" KPI link to the source submission.
    last_activity_submission_id: Optional[str] = None
    # The current state of `last_activity_submission_id` — one of
    # `running` | `success` | `failed`. Drives the color of the
    # "Last activity" timestamp on the form-summary KPI so the kind
    # of activity reads at a glance. Null when no events yet.
    last_activity_state: Optional[str] = None
    # Display-only labels from `@form(tags=[...])` — what the form
    # demonstrates or which area it belongs to. Rendered as a column
    # of small badges on the forms listing. Empty list when the
    # author didn't tag the form.
    tags: list[str] = Field(default_factory=list)
    # Origins permitted to embed this form in an iframe, from
    # `@form(iframe_allowed_origins=[...])`. Null when the form is
    # not iframable; surfaces in the admin UI as an icon column +
    # allowlist on the detail page. Same shape as the DSL field —
    # browser does the matching via CSP.
    iframe_allowed_origins: Optional[list[str]] = None


class SubmissionSummary(BaseModel):
    # Null only for a draft — but drafts are never persisted, so in
    # practice every listed submission has an id.
    submission_id: Optional[str] = None
    handle: str
    state: str
    # The form version (the human-facing integer) this submission ran on.
    form_version: int
    created_at: datetime
    # Last activity timestamp — bumped on every state change. Used
    # by the listing's "Last activity" column and the `updated_at`
    # sort. Always populated by the writer; only NULL on legacy
    # rows that predate the column (backfilled in the migration to
    # `terminated_at` or `created_at`, but the type stays Optional
    # for defense-in-depth).
    updated_at: Optional[datetime] = None
    terminated_at: Optional[datetime] = None
    # When non-null, the row is a tombstone — the row stays in the
    # listing only when `?show_deleted=1` is set AND the caller is
    # admin. The UI uses this signal to render a "deleted" pill and
    # disable the click-through (the in-memory submission was
    # evicted on soft-delete; the detail page would 404).
    deleted_at: Optional[datetime] = None
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
    # Tags + iframe_allowed_origins live on the live CompiledWorkflow
    # — pulled in here rather than persisted on the form_version row
    # because they describe what the *form* demonstrates / how it can
    # be embedded, not what a specific submission ran against. A form
    # whose source has been removed (is_live = False) gets nulls — the
    # FORMS registry no longer has it.
    out: list[FormSummary] = []
    for row in rows:
        wf = FORMS.get(row["form_id"])
        tags = list(wf.tags) if wf is not None else []
        iframe_origins = (
            list(wf.iframe_allowed_origins)
            if wf is not None and wf.iframe_allowed_origins
            else None
        )
        out.append(
            FormSummary(
                **row, tags=tags, iframe_allowed_origins=iframe_origins,
            )
        )
    return out


class SubmissionListingPage(BaseModel):
    """Page envelope for the form-submissions listing.

    The legacy shape was a flat `list[SubmissionSummary]`; this is
    the wrapped shape used by every UI listing tab going forward.
    `total` reflects the filtered row count (before limit/offset),
    so the UI can render an accurate "Showing M–N of T" footer and
    decide whether a Next button is enabled.
    """
    submissions: list[SubmissionSummary]
    total: int
    limit: int
    offset: int


def _parse_listing_bound(
    raw: Optional[str], *, end_of_day: bool,
) -> Optional[datetime]:
    """Parse a listing window bound.

    Accepts either a calendar date (`YYYY-MM-DD`) or a full ISO 8601
    datetime. Calendar dates are interpreted as UTC; for `end_of_day`
    bounds, the date is bumped to start-of-next-day so the half-open
    interval still includes everything stamped that day. ISO
    datetimes pass through unchanged after a tzinfo coerce.

    Returns None on:
      - empty string / None input — the caller treats as "no bound"
      - parse failure (malformed string) — same posture as other
        listing params: silently degrade rather than 400 a stale URL

    The two-format support is deliberate. The UI emits date-only
    bounds from `<input type="date">`. External integrations (and
    the future unified endpoint) hand in full ISO timestamps. One
    parser handles both.
    """
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    # Try date-only first; if that misses, fall back to full ISO.
    try:
        from datetime import date as _date
        parsed_date = _date.fromisoformat(text)
        dt = datetime.combine(
            parsed_date, datetime.min.time(),
        ).replace(tzinfo=timezone.utc)
        if end_of_day:
            # Half-open interval — bumping to start-of-next-day
            # makes "before=2025-06-30" actually include everything
            # stamped on 2025-06-30 (not exclude the day itself).
            dt = dt + timedelta(days=1)
        return dt
    except ValueError:
        pass
    try:
        # `fromisoformat` accepts the common ISO shapes Python emits.
        # "Z" suffix isn't accepted on 3.10; pre-strip for portability.
        normalized = text.replace("Z", "+00:00") if text.endswith("Z") else text
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


@api.get(
    "/forms/{form_id}/submissions",
    response_model=SubmissionListingPage,
)
def read_form_submissions(
    form_id: str,
    user: "store.User" = Depends(_current_user),
    limit: int = Query(default=25, ge=1),
    offset: int = Query(default=0, ge=0),
    state: list[str] = Query(default=[]),
    q: Optional[str] = Query(default=None, max_length=200),
    sort: list[str] = Query(default=[]),
    updated_since: Optional[str] = Query(default=None),
    updated_before: Optional[str] = Query(default=None),
    current_step: list[str] = Query(default=[]),
    show_deleted: bool = Query(default=False),
) -> SubmissionListingPage:
    """Paginated submissions of a form, with filter and sort.

    # TODO(unify-listing-and-export): see ROADMAP "Unify submission-
    # listing and submission-export endpoints". The listing
    # (offset/limit, calendar-date window, session auth, multi-col
    # sort) and the export (cursor, ISO-datetime window, bearer auth,
    # terminal-only) duplicate the same shape. Plan in ROADMAP.

    Query params
    ------------
    `limit` / `offset` — page window. `limit` clamped to [1,100].
    `state` — repeated; multi-select on submission state. Unknowns
              dropped.
    `q`     — substring match on submission_id OR handle, case-
              insensitive. Capped at 200 chars.
    `sort`  — repeated `column:direction`. Sortable: submission_id,
              state, form_version, created_at, updated_at.
              Multi-column = order of arrival; default
              `created_at:desc`.
    `updated_since` / `updated_before` — half-open interval on
              `Submission.updated_at`. Accept either calendar date
              (`YYYY-MM-DD`, UTC) or full ISO datetime. A single-day
              filter (`updated_since=2025-06-30&updated_before=
              2025-06-30`) captures everything stamped that day —
              the `before` bound is interpreted as start-of-next-day
              under the hood.
    `current_step` — repeated; multi-select on the submission's
              current step's node_id. Unknowns dropped.
    `show_deleted` — when True AND the user is admin, the listing
              also includes soft-deleted submissions (their
              `deleted_at` is populated; the UI renders a pill).
              Silently ignored for non-admins — they can never
              see tombstoned rows.

    Visibility:
      - admin                       → all submissions of this form
      - folder grant (view/manage)  → all submissions of this form
      - any other signed-in user    → only submissions they may view
      - anonymous                   → 401 (the `_current_user` dep)

    The visibility filter is applied as an IN-clause on the page
    query (not a post-paginate filter), so a limited user paginates
    over THEIR visible rows correctly rather than getting partially-
    filled pages.
    """
    if not store.form_exists(form_id):
        raise HTTPException(
            status_code=404, detail=f"form {form_id!r} not found"
        )

    # Parse sort spec. Each value is `column:direction`; malformed
    # entries are dropped silently rather than 400-ing, so a stale
    # URL from a UI refactor degrades to the default sort.
    sort_spec: list[tuple[str, str]] = []
    for entry in sort:
        col, _, direction = entry.partition(":")
        if col and direction:
            sort_spec.append((col, direction))

    # Date-or-ISO bounds. `end_of_day=True` on the upper bound bumps
    # a date-only value to start-of-next-day so a same-day window
    # includes that day's rows.
    since_dt = _parse_listing_bound(updated_since, end_of_day=False)
    before_dt = _parse_listing_bound(updated_before, end_of_day=True)
    # An inverted range (since > before) would silently return zero
    # rows, but that's the user's mistake; dropping one bound would
    # be a worse surprise. We just let it through.

    # show_deleted is admin-gated. Non-admins setting it has no
    # effect — they never see tombstones even if the URL says so.
    # Defense in depth on top of the listing tab's UI gate.
    include_deleted = bool(show_deleted) and bool(
        getattr(user, "is_admin", False)
    )

    visible_handles = _visible_submission_handles_for_user(form_id, user)
    page = store.list_form_submissions(
        form_id,
        limit=limit,
        offset=offset,
        states=state or None,
        query=q,
        sort=sort_spec or None,
        handle_whitelist=visible_handles,
        include_deleted=include_deleted,
        updated_since=since_dt,
        updated_before=before_dt,
        current_steps=current_step or None,
    )
    return SubmissionListingPage(
        submissions=[
            SubmissionSummary(**row) for row in page["submissions"]
        ],
        total=page["total"],
        limit=page["limit"],
        offset=page["offset"],
    )


# --- Preview endpoints ------------------------------------------------------
#
# Admins can step through a form without creating real submissions or
# triggering side effects. Preview submissions live in their own
# in-memory store (runtime._preview_submissions); they are never
# persisted, backends and external operators are stubbed, and
# branching is admin-driven via the pick endpoint.
#
# Sessions evict on user logout (revoke_session also clears them) or
# after the TTL (1 hour of inactivity). Identified by handle only.


# How long an idle preview lives in memory before being swept. The
# expense is small (one Submission per active admin) so we err on
# the side of "keeps working while the admin is reading docs in
# another tab" — an hour of idle is plenty.
PREVIEW_TTL = timedelta(hours=1)

# Last-seen timestamp per preview handle. A separate dict keeps the
# runtime store dependency-free; eviction is best-effort and runs
# opportunistically on each preview-related request.
_preview_last_seen: dict[str, datetime] = {}
_preview_owner: dict[str, int] = {}  # handle → user_id (admin)


def _touch_preview(handle: str, user_id: int) -> None:
    """Record activity on a preview handle and run an opportunistic
    sweep of expired sessions. Cheap — only a single timestamp write
    plus a dict walk over what's typically <10 entries."""
    now = datetime.now(timezone.utc)
    _preview_last_seen[handle] = now
    _preview_owner.setdefault(handle, user_id)
    # Sweep — drop any sessions that haven't been touched in TTL.
    stale = [
        h for h, ts in _preview_last_seen.items()
        if now - ts > PREVIEW_TTL
    ]
    for h in stale:
        delete_preview_submission(h)
        _preview_last_seen.pop(h, None)
        _preview_owner.pop(h, None)


def _get_preview_or_404(
    form_id: str, handle: str, user: "store.User",
) -> tuple[CompiledWorkflow, Submission]:
    """Resolve a preview submission, asserting it belongs to this
    admin. 404 if missing/expired; 403 if owned by someone else.
    """
    form = _get_form_or_404(form_id)
    sub = get_preview_submission(handle)
    if sub is None or sub.form_id != form_id:
        raise HTTPException(
            status_code=404,
            detail="preview session not found — it may have expired",
        )
    owner = _preview_owner.get(handle)
    if owner is not None and owner != user.id:
        raise HTTPException(
            status_code=403,
            detail="this preview session belongs to another admin",
        )
    _touch_preview(handle, user.id)
    return form, sub


class PreviewBranchChoiceRequest(BaseModel):
    """Admin's pick at a `@backend.branch` or `HitlBranch` decision
    point. `step_id` matches what the prior 422 response surfaced
    (the branch-owning step's id). `choice` is one of the offered
    downstream node ids, or the literal `"END"` to terminate."""
    step_id: str
    choice: str


class PreviewBranchPicker(BaseModel):
    """A picker the admin needs to resolve. Surfaced in the 422 body
    when a preview hits a branch decision the runtime can't make on
    its own."""
    step_id: str
    fn_name: str
    downstream: list[str]
    can_end: bool


def _preview_picker_response(
    exc: NeedsPreviewBranchChoice, submission: Submission,
    form: CompiledWorkflow,
) -> HTTPException:
    """Build a 422 with both the picker payload AND the current
    submission state so the frontend has everything it needs to
    render the page-with-overlay without an extra round trip."""
    base = _build_submission_response(submission, form).model_dump()
    base["preview_picker"] = PreviewBranchPicker(
        step_id=exc.step_id,
        fn_name=exc.fn_name,
        downstream=exc.downstream,
        can_end=exc.can_end,
    ).model_dump()
    return HTTPException(status_code=422, detail=base)


@api.post(
    "/forms/{form_id}/preview",
    response_model=SubmissionResponse,
    status_code=201,
    dependencies=[Depends(require_admin)],
)
def create_preview(
    form_id: str,
    req: StartSubmissionRequest,
    user: "store.User" = Depends(require_admin),
) -> SubmissionResponse:
    """Start a preview session for the form. Admin-only. No
    persistence, no backend execution, no side effects.

    Unlike the real `POST /submissions`, preview is permissive about
    the landing values: if a required field is missing, the runtime's
    auto-fill helper supplies a type-default so the preview can start
    without making the admin fill out anything first. The dropdown
    is always immediately usable. Admin-supplied values still win
    when present.
    """
    form = _get_form_or_404(form_id)
    # Start from the request values, then fill in any missing keys
    # with the auto-fill defaults. The dict ordering is: defaults
    # first, then admin values overlay them.
    from frontflow.dsl.runtime import _auto_fill_node_values
    defaults = _auto_fill_node_values(form.landing_node())
    merged_values = {**defaults, **dict(req.values or {})}
    # Same pruning logic the real endpoint uses — drop fields
    # hidden by an unsatisfied When.
    values = _prune_inactive(
        form.landing_node().fields, merged_values, {}
    )
    try:
        submission = start_submission(
            form,
            initial_form_values=values,
            form_version_id=FORM_VERSION_IDS.get(form_id, 0),
            preview=True,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _touch_preview(submission.handle, user.id)
    # Advance the runtime so the next step materializes (or the
    # submission terminates if the landing chain finishes the form).
    try:
        advance(form, submission)
    except NeedsPreviewBranchChoice as e:
        raise _preview_picker_response(e, submission, form) from e
    return _build_submission_response(submission, form)


@api.get(
    "/forms/{form_id}/preview/{handle}",
    response_model=SubmissionResponse,
    dependencies=[Depends(require_admin)],
)
def read_preview(
    form_id: str, handle: str,
    user: "store.User" = Depends(require_admin),
) -> SubmissionResponse:
    """Current state of a preview session. Idempotent — advances the
    runtime to ensure the latest step is materialized."""
    form, submission = _get_preview_or_404(form_id, handle, user)
    try:
        advance(form, submission)
    except NeedsPreviewBranchChoice as e:
        raise _preview_picker_response(e, submission, form) from e
    return _build_submission_response(submission, form)


@api.get(
    "/forms/{form_id}/preview/{handle}/steps/{step_id}",
    response_model=StepDetail,
    dependencies=[Depends(require_admin)],
)
def read_preview_step(
    form_id: str, handle: str, step_id: str,
    user: "store.User" = Depends(require_admin),
) -> StepDetail:
    """Layout + state for one step of a preview session. Mirrors the
    real read_step endpoint so the frontend renders previews with
    the same component."""
    form, submission = _get_preview_or_404(form_id, handle, user)
    try:
        advance(form, submission)
    except NeedsPreviewBranchChoice as e:
        raise _preview_picker_response(e, submission, form) from e

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
            detail=f"step {step_id!r} has not been reached in this preview",
        )

    response_payload: Optional[dict[str, Any]] = None
    if step.is_submitted:
        response_payload = {
            "values": dict(step.form_values or {}),
            "button": step.button_clicked,
        }
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
        page_id=page.id if page is not None else None,
        page_title=page.title if page is not None else None,
        error=step.error,
    )


@api.post(
    "/forms/{form_id}/preview/{handle}/steps/{step_id}",
    response_model=SubmissionResponse,
    dependencies=[Depends(require_admin)],
)
def submit_preview_step(
    form_id: str, handle: str, step_id: str,
    req: StepSubmissionRequest,
    user: "store.User" = Depends(require_admin),
) -> SubmissionResponse:
    """Submit a step of a preview session. Same semantics as the
    real submit endpoint — pruning, validation, button selection —
    but no persistence and no backend execution. May raise a
    NeedsPreviewBranchChoice, which surfaces as a 422 listing the
    available routes for the admin to pick from."""
    form, submission = _get_preview_or_404(form_id, handle, user)
    try:
        advance(form, submission)
    except NeedsPreviewBranchChoice as e:
        raise _preview_picker_response(e, submission, form) from e

    ng = form.all_nodes_by_id.get(step_id)
    if ng is None:
        if isinstance(form.by_id.get(step_id), CompiledBackendStep):
            raise HTTPException(
                status_code=404,
                detail=f"step {step_id!r} is a backend step — nothing to submit",
            )
        raise HTTPException(status_code=404, detail="step not found")

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
        steps_data = build_steps_with_workflow(form, submission)
        _validate_required_fields(ng, dict(req.values), steps_data)
        pruned = _prune_inactive(
            ng.fields, dict(req.values), steps_data
        )
        submit_step(
            form, submission,
            node_id=step_id, form_values=pruned,
            button_clicked=button_clicked,
        )
    except NeedsPreviewBranchChoice as e:
        raise _preview_picker_response(e, submission, form) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # advance() may itself hit another branch picker; surface it.
    try:
        advance(form, submission)
    except NeedsPreviewBranchChoice as e:
        raise _preview_picker_response(e, submission, form) from e
    return _build_submission_response(submission, form)


@api.post(
    "/forms/{form_id}/preview/{handle}/branch",
    response_model=SubmissionResponse,
    dependencies=[Depends(require_admin)],
)
def resolve_preview_branch_endpoint(
    form_id: str, handle: str,
    req: PreviewBranchChoiceRequest,
    user: "store.User" = Depends(require_admin),
) -> SubmissionResponse:
    """Record the admin's branch decision and re-drive the preview.
    `step_id` and `choice` come from the 422 picker payload."""
    form, submission = _get_preview_or_404(form_id, handle, user)
    try:
        resolve_preview_branch(form, submission, req.step_id, req.choice)
    except NeedsPreviewBranchChoice as e:
        # Another branch was hit after this one was resolved (rare,
        # but happens in chains with multiple branches). Surface it.
        raise _preview_picker_response(e, submission, form) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _build_submission_response(submission, form)


@api.delete(
    "/forms/{form_id}/preview/{handle}",
    status_code=204,
    dependencies=[Depends(require_admin)],
)
def delete_preview(
    form_id: str, handle: str,
    user: "store.User" = Depends(require_admin),
) -> Response:
    """Drop a preview session from memory. Idempotent — a missing
    handle is treated as success. The admin can also just abandon
    the session and let the TTL sweep evict it."""
    sub = get_preview_submission(handle)
    if sub is not None and sub.form_id != form_id:
        # Belongs to a different form — refuse so a stray DELETE
        # against the wrong form id doesn't drop the wrong session.
        raise HTTPException(
            status_code=404, detail="preview not found for this form"
        )
    owner = _preview_owner.get(handle)
    if owner is not None and owner != user.id:
        raise HTTPException(
            status_code=403,
            detail="this preview session belongs to another admin",
        )
    delete_preview_submission(handle)
    _preview_last_seen.pop(handle, None)
    _preview_owner.pop(handle, None)
    return Response(status_code=204)


class PreviewJumpRequest(BaseModel):
    """Fast-forward a preview to a named node. The target IS the
    branch decision for every branching node on the path — no extra
    picker prompts are needed."""
    target_node_id: str


@api.post(
    "/forms/{form_id}/preview/{handle}/jump",
    response_model=SubmissionResponse,
    dependencies=[Depends(require_admin)],
)
def jump_preview_endpoint(
    form_id: str, handle: str,
    req: PreviewJumpRequest,
    user: "store.User" = Depends(require_admin),
) -> SubmissionResponse:
    """Fast-forward the preview to `target_node_id`. Every step in
    between is auto-submitted with type-default values; branches on
    the path route deterministically toward the target.

    400 if the target doesn't exist, isn't reachable from the
    current frontier, or is already in the submission's history
    (jumping backward isn't supported — reset and re-jump).
    422 if the runtime hits a branch off the target path the
    pathfinder didn't anticipate; admin picks via the normal picker
    flow, then can re-call jump.
    """
    form, submission = _get_preview_or_404(form_id, handle, user)
    try:
        jump_preview(form, submission, req.target_node_id)
    except NeedsPreviewBranchChoice as e:
        raise _preview_picker_response(e, submission, form) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _build_submission_response(submission, form)


# --- Analytics endpoints ----------------------------------------------------
#
# Per-form aggregates that drive the Reports tab. Each endpoint owns
# one visualization's data. Filters arrive as query params and are
# parsed by `_parse_analytics_filters`; defaults come from the form's
# `@form(reports=...)` config (or the framework defaults when unset).
#
# The endpoint shape is per-chart, not bundled, so a filter change
# only re-fetches what depends on it. The endpoints share a small
# `_filter_submissions` helper rather than going through SQL: at
# expected scale (<100k submissions per form) Python aggregation is
# fast enough and avoids dialect-specific GROUP BY plumbing.


# Recognized state values. Surfaced in the response so the frontend
# renders all-bars-visible (with non-matching greyed out) per the
# filter-narrowing UX.
_KNOWN_SUBMISSION_STATES: tuple[str, ...] = (
    "queued", "running", "success", "failed",
)

# Named date-range presets the author can set as a default in
# `@form(reports={'default_filters': {'date_range': '...'}})`. The
# frontend's date-range picker emits explicit `start_date`/`end_date`
# query params; presets are an authoring convenience that expands to
# a concrete date pair at request time.
_DATE_RANGE_PRESETS: dict[str, int] = {
    "all_time": 0,           # 0 == no lower bound
    "last_7_days": 7,
    "last_30_days": 30,
    "last_90_days": 90,
}


class AnalyticsBucket(BaseModel):
    """One bar in a categorical chart. `key` is the bucket id the
    frontend filters on; `label` is for display; `count` is the bar
    height. The frontend uses `key` to drive URL-param filters when
    the bar is clicked."""
    key: str
    label: str
    count: int


class AnalyticsResponse(BaseModel):
    """Common shape for the categorical analytics endpoints. The
    `filters_applied` echo lets the frontend confirm which filters
    the server actually used (e.g. the resolved date range when a
    preset was expanded)."""
    buckets: list[AnalyticsBucket]
    total: int
    filters_applied: dict[str, Any]


def _resolve_default_filters(
    form: CompiledWorkflow,
) -> dict[str, Any]:
    """Merge framework defaults with the form's `reports` overrides.
    Returned dict is the *fully resolved* default filter set used
    when a query param is absent.

    Default state filter is `['success', 'failed']` — the analytics
    page focuses on completed work by default. Authors can override
    via `@form(reports={'default_filters': {'state': None}})` to
    show every state, or pass any subset to scope the page.
    """
    framework_defaults: dict[str, Any] = {
        "date_range": "last_30_days",
        "state": ["success", "failed"],
        "current_step": None,  # None = all current steps
    }
    overrides = (form.reports or {}).get("default_filters") or {}
    framework_defaults.update(overrides)
    return framework_defaults


def _parse_analytics_filters(
    form: CompiledWorkflow,
    *,
    state: Optional[list[str]] = None,
    current_step: Optional[list[str]] = None,
    date_range: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve query-param filters against form defaults. Explicit
    `start_date` / `end_date` take precedence over a `date_range`
    preset, so a custom-range UI can override what the form author
    picked as the default."""
    defaults = _resolve_default_filters(form)

    # Date range — explicit pair wins; otherwise preset; otherwise
    # default preset. Returns a `(start, end)` tuple of UTC datetimes
    # or `(None, None)` to mean "no bound."
    now = datetime.now(timezone.utc)
    if start_date or end_date:
        # Parse ISO dates. Invalid -> 400 with a clear message.
        start_dt = _parse_iso_date(start_date) if start_date else None
        end_dt = _parse_iso_date(end_date) if end_date else None
    else:
        preset = date_range or defaults.get("date_range") or "all_time"
        if preset not in _DATE_RANGE_PRESETS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown date_range {preset!r} — must be one of "
                    f"{sorted(_DATE_RANGE_PRESETS)}, or use "
                    f"start_date/end_date for a custom range"
                ),
            )
        days = _DATE_RANGE_PRESETS[preset]
        start_dt = (now - timedelta(days=days)) if days > 0 else None
        end_dt = None  # ranges are "from X up to now"

    return {
        "state": state if state is not None else defaults.get("state"),
        "current_step": current_step if current_step is not None
            else defaults.get("current_step"),
        "start_date": start_dt,
        "end_date": end_dt,
        # Pass the preset name through too so the frontend can
        # round-trip the picker selection without re-deriving.
        "date_range_preset": (
            None if (start_date or end_date)
            else (date_range or defaults.get("date_range") or "all_time")
        ),
    }


def _parse_iso_date(s: str) -> datetime:
    """Parse an ISO date or datetime, normalize to UTC. Accepts
    `2025-01-15`, `2025-01-15T00:00:00Z`, etc."""
    try:
        if "T" in s:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"invalid date {s!r}: {e}",
        )


def _filter_submissions(
    submissions: list[dict[str, Any]],
    filters: dict[str, Any],
) -> list[dict[str, Any]]:
    """Apply the resolved filter set to a list of submission rows.
    Each row is the dict shape returned by `store.list_form_submissions`."""
    out = submissions
    if filters.get("state"):
        wanted = set(filters["state"])
        out = [r for r in out if r["state"] in wanted]
    if filters.get("current_step"):
        wanted_steps = set(filters["current_step"])
        out = [r for r in out if r["current_step"] in wanted_steps]
    if filters.get("start_date"):
        start = filters["start_date"]
        out = [r for r in out if r["created_at"] and r["created_at"] >= start]
    if filters.get("end_date"):
        end = filters["end_date"]
        out = [r for r in out if r["created_at"] and r["created_at"] <= end]
    return out


def _filters_for_response(filters: dict[str, Any]) -> dict[str, Any]:
    """JSON-serializable form of the resolved filter set for echoing
    back in the response."""
    return {
        "state": filters.get("state"),
        "current_step": filters.get("current_step"),
        "start_date": (
            filters["start_date"].isoformat()
            if filters.get("start_date") else None
        ),
        "end_date": (
            filters["end_date"].isoformat()
            if filters.get("end_date") else None
        ),
        "date_range_preset": filters.get("date_range_preset"),
    }


def _load_form_or_404(form_id: str) -> CompiledWorkflow:
    if form_id not in FORMS:
        raise HTTPException(
            status_code=404, detail=f"form {form_id!r} not found"
        )
    return FORMS[form_id]


class AnalyticsDefaultsResponse(BaseModel):
    """The form's resolved default analytics filters — framework
    defaults merged with `@form(reports={'default_filters': ...})`
    overrides. The Reports tab fetches this on mount and seeds the
    URL with the values so the URL is always the source of truth for
    "what filters are active." A user landing on a bare page URL
    immediately gets the defaults written to the query string,
    making the page state visible and shareable.
    """
    state: Optional[list[str]] = None
    current_step: Optional[list[str]] = None
    date_range: Optional[str] = None


@api.get(
    "/forms/{form_id}/analytics/defaults",
    response_model=AnalyticsDefaultsResponse,
    dependencies=[Depends(require_form_access("view"))],
)
def analytics_defaults(form_id: str) -> AnalyticsDefaultsResponse:
    """Return the resolved default analytics filters for this form.
    Used by the Reports tab on mount to seed URL query params so the
    URL always reflects the active filter set."""
    form = _load_form_or_404(form_id)
    d = _resolve_default_filters(form)
    return AnalyticsDefaultsResponse(
        state=d.get("state"),
        current_step=d.get("current_step"),
        date_range=d.get("date_range"),
    )


@api.get(
    "/forms/{form_id}/analytics/state",
    response_model=AnalyticsResponse,
    dependencies=[Depends(require_form_access("view"))],
)
def analytics_state(
    form_id: str,
    state: Optional[list[str]] = Query(default=None),
    current_step: Optional[list[str]] = Query(default=None),
    date_range: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> AnalyticsResponse:
    """Submissions grouped by state. Every known state appears as a
    bucket, including those with count 0, so the frontend can render
    all bars and grey out the empty ones — keeps the chart shape
    stable across filter toggles.

    The state filter is *not* applied to this endpoint — that would
    be circular (the chart's whole job is to show the distribution
    of submissions across states; filtering by state first would
    just zero out the non-matching bars). Other filters (date
    range, current_step) still apply so the chart respects the
    other page-level narrowing.
    """
    form = _load_form_or_404(form_id)
    filters = _parse_analytics_filters(
        form,
        state=state, current_step=current_step,
        date_range=date_range,
        start_date=start_date, end_date=end_date,
    )
    # Drop the chart's own dimension — see docstring.
    own_dim_dropped = {**filters, "state": None}
    rows = _filter_submissions(
        store.list_all_form_submissions(form_id), own_dim_dropped,
    )
    counts: dict[str, int] = {s: 0 for s in _KNOWN_SUBMISSION_STATES}
    for r in rows:
        s = r["state"]
        if s in counts:
            counts[s] += 1
        else:
            # Unknown state — surface it rather than swallow.
            counts[s] = counts.get(s, 0) + 1
    return AnalyticsResponse(
        buckets=[
            AnalyticsBucket(key=k, label=k.replace("_", " "), count=c)
            for k, c in counts.items()
        ],
        total=len(rows),
        filters_applied=_filters_for_response(filters),
    )


@api.get(
    "/forms/{form_id}/analytics/current_step",
    response_model=AnalyticsResponse,
    dependencies=[Depends(require_form_access("view"))],
)
def analytics_current_step(
    form_id: str,
    state: Optional[list[str]] = Query(default=None),
    current_step: Optional[list[str]] = Query(default=None),
    date_range: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> AnalyticsResponse:
    """Submissions grouped by their current step, terminal nodes
    filtered out. Answers "where are submissions stuck?". Terminal
    nodes are nodes with no downstream edges — once a submission
    reaches one, it's no longer "in progress" at any step.

    The current_step filter is *not* applied to this endpoint, by
    the same excludes-self rule as analytics_state — the chart's
    own dimension would be circular if filtered.
    """
    form = _load_form_or_404(form_id)
    filters = _parse_analytics_filters(
        form,
        state=state, current_step=current_step,
        date_range=date_range,
        start_date=start_date, end_date=end_date,
    )
    own_dim_dropped = {**filters, "current_step": None}
    rows = _filter_submissions(
        store.list_all_form_submissions(form_id), own_dim_dropped,
    )
    # Build the set of terminal node ids — nodes with no downstream.
    terminal_ids = {
        n.id for n in form.all_nodes_by_id.values()
        if not getattr(n, "downstream", None)
    }
    counts: dict[str, int] = {}
    for n in form.all_nodes_by_id.values():
        if n.id not in terminal_ids:
            counts[n.id] = 0
    for r in rows:
        cs = r["current_step"]
        if cs is None or cs in terminal_ids:
            continue
        counts[cs] = counts.get(cs, 0) + 1
    # Resolve display labels: node title when available, else id.
    return AnalyticsResponse(
        buckets=[
            AnalyticsBucket(
                key=node_id,
                label=(
                    form.all_nodes_by_id[node_id].title
                    if node_id in form.all_nodes_by_id
                    and form.all_nodes_by_id[node_id].title
                    else node_id
                ),
                count=c,
            )
            for node_id, c in counts.items()
        ],
        total=sum(counts.values()),
        filters_applied=_filters_for_response(filters),
    )


@api.get(
    "/forms/{form_id}/analytics/step_counts",
    response_model=AnalyticsResponse,
    dependencies=[Depends(require_form_access("view"))],
)
def analytics_step_counts(
    form_id: str,
    state: Optional[list[str]] = Query(default=None),
    current_step: Optional[list[str]] = Query(default=None),
    date_range: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> AnalyticsResponse:
    """How many submissions ever *reached* each step. Different from
    current_step (which is parking position): this counts everyone
    who got there, including those who moved past. Useful for
    branch-counting questions like "how many uploaded csv vs xlsx"
    — both branches' downstream nodes show their reach counts.

    The count is computed from each submission's step history (its
    full list of node visits), not just its current step. Re-entries
    via edit cascade are counted once per submission per node, not
    once per visit."""
    form = _load_form_or_404(form_id)
    filters = _parse_analytics_filters(
        form,
        state=state, current_step=current_step,
        date_range=date_range,
        start_date=start_date, end_date=end_date,
    )
    rows = _filter_submissions(
        store.list_all_form_submissions(form_id), filters,
    )
    # Initialize every node to 0 so the chart shows a stable axis even
    # when some nodes are never reached by the filtered submissions.
    counts: dict[str, int] = {n.id: 0 for n in form.all_nodes_by_id.values()}
    # We need the full step history per submission; load it. The list-
    # endpoint only returned the current step, so this requires a
    # second store call. Acceptable for the analytics path — large
    # forms can refine the filter set to bound the cost.
    for r in rows:
        sub_id = r["submission_id"] or r["handle"]
        history = store.list_submission_step_history(form_id, sub_id)
        seen = set()
        for node_id in history:
            if node_id in counts and node_id not in seen:
                counts[node_id] += 1
                seen.add(node_id)
    return AnalyticsResponse(
        buckets=[
            AnalyticsBucket(
                key=node_id,
                label=(
                    form.all_nodes_by_id[node_id].title
                    if form.all_nodes_by_id[node_id].title
                    else node_id
                ),
                count=c,
            )
            for node_id, c in counts.items()
        ],
        total=len(rows),
        filters_applied=_filters_for_response(filters),
    )


class FlowNode(BaseModel):
    """One node in the submission-flow sankey. `reach` is the total
    submissions that ever reached this node (matches step_counts).
    `terminals` and `outgoing` together account for *what happened
    to those submissions at this node* — `reach` == sum(terminals'
    values) + sum(outgoing edges from this node)."""
    node_id: str
    label: str
    reach: int
    # Submissions that stopped at this node, broken out by fate.
    # `failed` = the chain failed here. `in_flight` = the
    # submission is currently parked here (running or awaiting).
    # Keys absent when their counts are zero so the frontend doesn't
    # render empty sub-stacks.
    terminals: dict[str, int]


class FlowEdge(BaseModel):
    """One edge in the form's graph, with a count of submissions that
    actually flowed across it (in the filtered set). Source + target
    are node ids; the frontend will key sub-slices off `source` to
    decide which outgoing slice this edge attaches to."""
    source: str
    target: str
    count: int


class FlowResponse(BaseModel):
    nodes: list[FlowNode]
    edges: list[FlowEdge]
    total: int
    filters_applied: dict[str, Any]


@api.get(
    "/forms/{form_id}/analytics/flow",
    response_model=FlowResponse,
    dependencies=[Depends(require_form_access("view"))],
)
def analytics_flow(
    form_id: str,
    state: Optional[list[str]] = Query(default=None),
    current_step: Optional[list[str]] = Query(default=None),
    date_range: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> FlowResponse:
    """Per-node reach counts + per-node terminal breakdown + per-edge
    flow counts for the submission-flow sankey. Re-entries via edit
    cascade count once per submission per node (matches the
    step_counts semantics).

    Edge counts are derived from unique adjacent pairs in each
    submission's step history, filtered to transitions that *exist*
    in the form's graph — a re-entry that walks backward in the
    history (e.g. `... C, B, ...` after edit) produces a `C→B`
    adjacency that isn't a real form edge; we drop those rather
    than double-count or distort the diagram.

    The current_step filter is *not* applied — the sankey's own
    dimension is the node a submission passes through; filtering by
    it would collapse the flow to only the matching node's lineage.
    Other filters (state, date) still apply.
    """
    form = _load_form_or_404(form_id)
    filters = _parse_analytics_filters(
        form,
        state=state, current_step=current_step,
        date_range=date_range,
        start_date=start_date, end_date=end_date,
    )
    own_dim_dropped = {**filters, "current_step": None}
    rows = _filter_submissions(
        store.list_all_form_submissions(form_id), own_dim_dropped,
    )

    # The sankey runs at the *user-visible* level — pages and top-
    # level nodes — not at the section-node granularity inside pages.
    # `form.steps` is exactly that list; their `.downstream` holds
    # the cross-step `>>` wiring that matches what `step.node_id`
    # records in the database.
    step_ids = [s.id for s in form.steps]
    step_titles: dict[str, str] = {
        s.id: (getattr(s, "title", None) or s.id) for s in form.steps
    }

    # Build the form's edge set. Used to filter spurious adjacency
    # pairs (e.g. re-entries via edit cascade can produce a `C→B`
    # adjacency in history that isn't a real form edge) and to seed
    # the response with zero-count edges so the diagram structure
    # stays stable across filter changes.
    form_edges: set[tuple[str, str]] = set()
    for s in form.steps:
        for ds in getattr(s, "downstream", []) or []:
            form_edges.add((s.id, ds))

    # Per-step accumulators, all seeded at 0 so the response axis is
    # stable.
    reach: dict[str, int] = {sid: 0 for sid in step_ids}
    edge_counts: dict[tuple[str, str], int] = {e: 0 for e in form_edges}
    terminals: dict[str, dict[str, int]] = {sid: {} for sid in step_ids}

    # Section-node ids inside pages collapse to the owning page's id
    # when classifying step history — the user-facing sankey sees
    # the page, not its internal sections. For a flat single-section
    # page where section id == page id, this is a no-op; for multi-
    # section pages it's the right thing.
    section_to_page: dict[str, str] = {
        sn_id: page.id
        for sn_id, page in form.node_page.items()
    }

    def normalize(node_id: str) -> Optional[str]:
        # Map section-node ids up to their owning page id, then check
        # the result is a real top-level step id. Returns None for
        # ids we don't know about (defensive).
        nid = section_to_page.get(node_id, node_id)
        return nid if nid in reach else None

    for r in rows:
        sub_id = r["submission_id"] or r["handle"]
        raw_history = store.list_submission_step_history(form_id, sub_id)
        if not raw_history:
            continue
        # Project section ids up to page ids; drop unknown ids.
        history = [
            nid for nid in (normalize(h) for h in raw_history) if nid
        ]
        if not history:
            continue
        # Reach: dedupe per submission so re-entries via edit cascade
        # don't double-count.
        seen_nodes: set[str] = set()
        for node_id in history:
            if node_id not in seen_nodes:
                reach[node_id] += 1
                seen_nodes.add(node_id)
        # Edges: unique adjacent pairs that exist as real form edges.
        # Adjacencies that aren't in the form's graph (e.g. a backward
        # re-entry from an edit cascade) are dropped — counting them
        # would either invent edges the diagram has no place for or
        # distort the real ones.
        seen_edges: set[tuple[str, str]] = set()
        for a, b in zip(history, history[1:]):
            if a == b:
                continue  # consecutive duplicates after collapsing — skip
            pair = (a, b)
            if pair in form_edges and pair not in seen_edges:
                edge_counts[pair] += 1
                seen_edges.add(pair)
        # Terminal sub-slice at the submission's last node. Failed
        # submissions land in `failed`; queued/running ones in
        # `in_flight`; successful ones in `succeeded`. The succeeded
        # bucket only meaningfully fills at form-terminal nodes
        # (nodes with no outgoing edges), since a `success` state
        # can't co-occur with "still has somewhere to go" — but the
        # classification is by submission state, not by node, so we
        # don't enforce it here.
        last_node = history[-1]
        sub_state = r["state"]
        if sub_state == "failed":
            terminals[last_node]["failed"] = (
                terminals[last_node].get("failed", 0) + 1
            )
        elif sub_state in ("queued", "running"):
            terminals[last_node]["in_flight"] = (
                terminals[last_node].get("in_flight", 0) + 1
            )
        elif sub_state == "success":
            terminals[last_node]["succeeded"] = (
                terminals[last_node].get("succeeded", 0) + 1
            )

    nodes = [
        FlowNode(
            node_id=sid,
            label=step_titles[sid],
            reach=reach[sid],
            terminals=terminals[sid],
        )
        for sid in step_ids
    ]
    edges = [
        FlowEdge(source=s, target=t, count=c)
        for (s, t), c in edge_counts.items()
    ]
    return FlowResponse(
        nodes=nodes,
        edges=edges,
        total=len(rows),
        filters_applied=_filters_for_response(filters),
    )


# --- Time-series analytics --------------------------------------------------
#
# Three time-shaped endpoints feed the second half of the Reports tab:
#   - completion_time: histogram of total time-to-terminate for
#     completed submissions in the filtered set
#   - step_time: per-step in-step duration aggregates (count, mean,
#     p10, p50, p90) for drill-down into where time goes
#   - throughput: submissions started per time bucket, stacked by
#     terminal state at query time
#
# All three accept the same filters as the other analytics endpoints;
# the framework default `state=['success','failed']` means time
# charts focus on completed work by default. An author can override
# via `@form(reports={'default_filters': {'state': None}})`.


class HistogramBucket(BaseModel):
    """One bar in a duration histogram. The interval is half-open —
    `[lo_seconds, hi_seconds)` — so adjacent buckets don't overlap.
    `label` is a pre-formatted human-readable range for display."""
    lo_seconds: float
    hi_seconds: float
    label: str
    count: int


class CompletionTimeResponse(BaseModel):
    buckets: list[HistogramBucket]
    total: int
    # Summary statistics over the unbucketed data — useful for a
    # caption beside the histogram. Always in seconds.
    mean_seconds: Optional[float] = None
    p50_seconds: Optional[float] = None
    p90_seconds: Optional[float] = None
    # Diagnostics — surface why the histogram might be empty.
    # `matching_submissions` is the count after applying filters;
    # `with_terminated_at` is the subset that also has a
    # terminated_at timestamp (the population the histogram is
    # computed over). When `matching_submissions > 0` but
    # `with_terminated_at == 0`, the chart can show a more useful
    # empty-state message than the bare "no completed submissions".
    matching_submissions: int = 0
    with_terminated_at: int = 0
    filters_applied: dict[str, Any]


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile over a pre-sorted list. `pct`
    is in [0, 100]. Returns 0 for an empty input — the caller is
    expected to guard against that case at display time."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (pct / 100) * (len(sorted_values) - 1)
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = k - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def _bucket_durations(
    durations_s: list[float], n_buckets: int = 20
) -> list[HistogramBucket]:
    """Linear-spaced histogram. The bucket count is fixed at 20
    because that's roughly the visual sweet spot for histograms at
    a typical chart width — fine enough to show shape, coarse
    enough not to look noisy. Empty input → empty histogram."""
    if not durations_s:
        return []
    lo = min(durations_s)
    hi = max(durations_s)
    if lo == hi:
        # Degenerate case — every submission took the same amount of
        # time. One bucket with that count, labeled as the exact
        # value.
        return [
            HistogramBucket(
                lo_seconds=lo,
                hi_seconds=hi,
                label=_format_duration(lo),
                count=len(durations_s),
            )
        ]
    width = (hi - lo) / n_buckets
    buckets = [
        {"lo": lo + i * width, "hi": lo + (i + 1) * width, "count": 0}
        for i in range(n_buckets)
    ]
    for d in durations_s:
        # The last bucket is closed on both ends so the maximum
        # value lands in it (clamp to n_buckets - 1).
        idx = min(n_buckets - 1, int((d - lo) / width))
        buckets[idx]["count"] += 1
    return [
        HistogramBucket(
            lo_seconds=b["lo"],
            hi_seconds=b["hi"],
            label=f"{_format_duration(b['lo'])}–{_format_duration(b['hi'])}",
            count=b["count"],
        )
        for b in buckets
    ]


def _format_duration(seconds: float) -> str:
    """Human-friendly duration formatting. Trades exact precision
    for readability at chart-axis scale: sub-minute durations show
    as seconds, sub-hour as minutes, sub-day as hours, then days."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


@api.get(
    "/forms/{form_id}/analytics/completion_time",
    response_model=CompletionTimeResponse,
    dependencies=[Depends(require_form_access("view"))],
)
def analytics_completion_time(
    form_id: str,
    state: Optional[list[str]] = Query(default=None),
    current_step: Optional[list[str]] = Query(default=None),
    date_range: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> CompletionTimeResponse:
    """Distribution of submission lifetime — `terminated_at -
    created_at` — for submissions that have actually terminated.
    In-flight submissions are skipped regardless of filters (they
    have no completion time). Returns a 20-bucket histogram plus
    summary stats for the caption."""
    form = _load_form_or_404(form_id)
    filters = _parse_analytics_filters(
        form,
        state=state, current_step=current_step,
        date_range=date_range,
        start_date=start_date, end_date=end_date,
    )
    rows = _filter_submissions(
        store.list_all_form_submissions(form_id), filters,
    )
    # Only completed submissions contribute. A submission can be in
    # `state == 'failed'` and have `terminated_at` set; both `success`
    # and `failed` count. (A `running` submission with the state
    # filter overridden to include it would be skipped here too.)
    durations: list[float] = []
    n_with_terminated = 0
    for r in rows:
        ta = r.get("terminated_at")
        ca = r.get("created_at")
        if ta and ca:
            durations.append((ta - ca).total_seconds())
            n_with_terminated += 1
    durations.sort()
    return CompletionTimeResponse(
        buckets=_bucket_durations(durations),
        total=len(durations),
        mean_seconds=(sum(durations) / len(durations)) if durations else None,
        p50_seconds=_percentile(durations, 50) if durations else None,
        p90_seconds=_percentile(durations, 90) if durations else None,
        matching_submissions=len(rows),
        with_terminated_at=n_with_terminated,
        filters_applied=_filters_for_response(filters),
    )


class StepTimeBucket(BaseModel):
    """Aggregate timing for one step. `count` is the number of
    *completed* visits — visits that have both a started_at and a
    submitted_at — so an in-flight current step doesn't skew the
    mean. The percentiles are in the same population."""
    node_id: str
    label: str
    count: int
    mean_seconds: float
    p10_seconds: float
    p50_seconds: float
    p90_seconds: float


class StepTimeResponse(BaseModel):
    steps: list[StepTimeBucket]
    filters_applied: dict[str, Any]


@api.get(
    "/forms/{form_id}/analytics/step_time",
    response_model=StepTimeResponse,
    dependencies=[Depends(require_form_access("view"))],
)
def analytics_step_time(
    form_id: str,
    state: Optional[list[str]] = Query(default=None),
    current_step: Optional[list[str]] = Query(default=None),
    date_range: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> StepTimeResponse:
    """Per-step time-in-step aggregates. For each step, the duration
    of one visit is `submitted_at - started_at`; we collect every
    completed visit across the filtered submissions and report
    count + mean + p10/p50/p90 per node.

    Re-entries via edit cascade each count as a separate visit —
    different from `step_counts` which dedupes per submission. The
    operational question here is "how long does this step take per
    attempt," and an edit-cascade re-run is a real second attempt
    with its own user time."""
    form = _load_form_or_404(form_id)
    filters = _parse_analytics_filters(
        form,
        state=state, current_step=current_step,
        date_range=date_range,
        start_date=start_date, end_date=end_date,
    )
    rows = _filter_submissions(
        store.list_all_form_submissions(form_id), filters,
    )
    # Collect durations per step. Only completed visits (both
    # timestamps present) contribute — an awaiting current step on
    # an in-flight submission is excluded.
    per_step: dict[str, list[float]] = {}
    for r in rows:
        sub_id = r["submission_id"] or r["handle"]
        timing = store.list_submission_step_timing(form_id, sub_id)
        for st in timing:
            if st["started_at"] and st["submitted_at"]:
                d = (st["submitted_at"] - st["started_at"]).total_seconds()
                per_step.setdefault(st["node_id"], []).append(d)
    # Build the response in form-step order so the chart's vertical
    # axis matches the workflow order the author declared. Steps
    # with no completed visits in this set are omitted — including
    # them as zero-mean would be misleading.
    out: list[StepTimeBucket] = []
    for s in form.steps:
        ds = per_step.get(s.id)
        if not ds:
            continue
        ds_sorted = sorted(ds)
        out.append(
            StepTimeBucket(
                node_id=s.id,
                label=(getattr(s, "title", None) or s.id),
                count=len(ds_sorted),
                mean_seconds=sum(ds_sorted) / len(ds_sorted),
                p10_seconds=_percentile(ds_sorted, 10),
                p50_seconds=_percentile(ds_sorted, 50),
                p90_seconds=_percentile(ds_sorted, 90),
            )
        )
    return StepTimeResponse(
        steps=out,
        filters_applied=_filters_for_response(filters),
    )


class StepHistogramResponse(BaseModel):
    """Per-step histogram drill-down — same shape as completion_time
    but scoped to one node's visit durations. Driven by the time-
    per-step chart's bar-click."""
    node_id: str
    label: str
    buckets: list[HistogramBucket]
    total: int
    mean_seconds: Optional[float] = None
    p50_seconds: Optional[float] = None
    p90_seconds: Optional[float] = None
    filters_applied: dict[str, Any]


@api.get(
    "/forms/{form_id}/analytics/step_time/{node_id}",
    response_model=StepHistogramResponse,
    dependencies=[Depends(require_form_access("view"))],
)
def analytics_step_time_one(
    form_id: str,
    node_id: str,
    state: Optional[list[str]] = Query(default=None),
    current_step: Optional[list[str]] = Query(default=None),
    date_range: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> StepHistogramResponse:
    """Drill-down histogram for one step's visit-duration distribution.
    Reached by clicking a bar in the time-per-step chart. Same
    filters apply as elsewhere, so the histogram reflects whatever
    page-level narrowing the user has set."""
    form = _load_form_or_404(form_id)
    if node_id not in {s.id for s in form.steps}:
        raise HTTPException(
            status_code=404, detail=f"step {node_id!r} not in form",
        )
    filters = _parse_analytics_filters(
        form,
        state=state, current_step=current_step,
        date_range=date_range,
        start_date=start_date, end_date=end_date,
    )
    rows = _filter_submissions(
        store.list_all_form_submissions(form_id), filters,
    )
    durations: list[float] = []
    for r in rows:
        sub_id = r["submission_id"] or r["handle"]
        for st in store.list_submission_step_timing(form_id, sub_id):
            if (
                st["node_id"] == node_id
                and st["started_at"] and st["submitted_at"]
            ):
                durations.append(
                    (st["submitted_at"] - st["started_at"]).total_seconds()
                )
    durations.sort()
    label = next(
        (getattr(s, "title", None) or s.id
         for s in form.steps if s.id == node_id),
        node_id,
    )
    return StepHistogramResponse(
        node_id=node_id,
        label=label,
        buckets=_bucket_durations(durations),
        total=len(durations),
        mean_seconds=(sum(durations) / len(durations)) if durations else None,
        p50_seconds=_percentile(durations, 50) if durations else None,
        p90_seconds=_percentile(durations, 90) if durations else None,
        filters_applied=_filters_for_response(filters),
    )


class ThroughputBucket(BaseModel):
    """One time bucket on the throughput chart. `start` is the bucket
    boundary (ISO datetime); `counts` is a {state -> count} map
    using submission state at query time."""
    start: str
    counts: dict[str, int]


class ThroughputResponse(BaseModel):
    buckets: list[ThroughputBucket]
    interval: Literal["day", "week", "month"]
    total: int
    filters_applied: dict[str, Any]


def _resolve_throughput_interval(
    interval: Optional[str], filters: dict[str, Any],
) -> Literal["day", "week", "month"]:
    """Auto-pick a time bucket from the resolved date range when the
    caller doesn't specify one. Last-week-ish ranges → day; last-
    quarter-ish → week; longer → month. The caller can override via
    the `interval` query param."""
    if interval in ("day", "week", "month"):
        return interval
    start = filters.get("start_date")
    end = filters.get("end_date") or datetime.now(timezone.utc)
    if not start:
        # `all_time` — pick monthly so the chart doesn't sprawl.
        return "month"
    span_days = (end - start).total_seconds() / 86400
    if span_days <= 14:
        return "day"
    if span_days <= 120:
        return "week"
    return "month"


def _bucket_start(dt: datetime, interval: str) -> datetime:
    """Floor a datetime to the start of its bucket. Week buckets
    start on Monday (ISO convention). Month buckets start on the
    1st. Day buckets start at 00:00 UTC."""
    dt = dt.astimezone(timezone.utc)
    if interval == "day":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if interval == "week":
        d = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return d - timedelta(days=d.weekday())
    # month
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_bucket(dt: datetime, interval: str) -> datetime:
    """Advance to the next bucket's start."""
    if interval == "day":
        return dt + timedelta(days=1)
    if interval == "week":
        return dt + timedelta(weeks=1)
    # month — naive month arithmetic that handles year rollover.
    if dt.month == 12:
        return dt.replace(year=dt.year + 1, month=1)
    return dt.replace(month=dt.month + 1)


@api.get(
    "/forms/{form_id}/analytics/throughput",
    response_model=ThroughputResponse,
    dependencies=[Depends(require_form_access("view"))],
)
def analytics_throughput(
    form_id: str,
    state: Optional[list[str]] = Query(default=None),
    current_step: Optional[list[str]] = Query(default=None),
    date_range: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    interval: Optional[str] = None,
) -> ThroughputResponse:
    """Submissions started per time bucket, stacked by state at
    query time. The chart's x-axis is `created_at` (when the
    submission started), color is current state, so past buckets
    show the present-day distribution of submissions that started
    then. As in-flight submissions terminate over time, past
    buckets re-color away from `in_flight` toward `success`/`failed`.

    The bucket interval is auto-picked from the date range unless
    `interval` (day | week | month) is explicit."""
    form = _load_form_or_404(form_id)
    filters = _parse_analytics_filters(
        form,
        state=state, current_step=current_step,
        date_range=date_range,
        start_date=start_date, end_date=end_date,
    )
    chosen_interval = _resolve_throughput_interval(interval, filters)
    rows = _filter_submissions(
        store.list_all_form_submissions(form_id), filters,
    )
    if not rows:
        return ThroughputResponse(
            buckets=[], interval=chosen_interval, total=0,
            filters_applied=_filters_for_response(filters),
        )
    # Group by bucket.
    by_bucket: dict[datetime, dict[str, int]] = {}
    for r in rows:
        ca = r.get("created_at")
        if not ca:
            continue
        start_b = _bucket_start(ca, chosen_interval)
        st = r["state"]
        by_bucket.setdefault(start_b, {})
        by_bucket[start_b][st] = by_bucket[start_b].get(st, 0) + 1
    # Fill in any empty buckets between the earliest and latest so
    # the chart's x-axis has no gaps — a zero-throughput period
    # should read as flat, not missing.
    if by_bucket:
        cursor = min(by_bucket.keys())
        end = max(by_bucket.keys())
        while cursor < end:
            by_bucket.setdefault(cursor, {})
            cursor = _next_bucket(cursor, chosen_interval)
    out = [
        ThroughputBucket(start=b.isoformat(), counts=c)
        for b, c in sorted(by_bucket.items())
    ]
    return ThroughputResponse(
        buckets=out,
        interval=chosen_interval,
        total=len(rows),
        filters_applied=_filters_for_response(filters),
    )


# --- Submission rate (fine-grained, for activity/security monitoring) -------
#
# Same x-axis idea as throughput, but at minute-level resolution and
# without state stacking — the question is "did volume spike?", and
# state breakdown distributes that signal across colors and buries
# it. Buckets run from 1-minute through 1-month so the chart can
# show both short bursts (attack traffic) and long-range baselines
# (capacity planning).


# Recognized bucket sizes for the rate endpoint. Maps the URL/auto
# names to (timedelta, sortable index). Months don't have a fixed
# timedelta — handled separately in the bucket-floor logic.
_RATE_INTERVAL_SECONDS: dict[str, int] = {
    "minute": 60,
    "5min": 300,
    "15min": 900,
    "hour": 3600,
    "day": 86400,
    "week": 604800,
    "month": 0,  # special-cased
}

_RATE_INTERVAL_ORDER = list(_RATE_INTERVAL_SECONDS.keys())


def _resolve_rate_interval(
    interval: Optional[str], filters: dict[str, Any],
) -> str:
    """Auto-pick a fine-grained bucket size from the resolved date
    range when the caller doesn't specify one. Tunes toward keeping
    the bar count manageable: roughly 60–300 buckets per range, with
    a strong preference for sub-hour resolution on short ranges
    (the attack-detection use case).
    """
    if interval and interval in _RATE_INTERVAL_SECONDS:
        return interval
    start = filters.get("start_date")
    end = filters.get("end_date") or datetime.now(timezone.utc)
    if not start:
        # `all_time` — go monthly so the chart doesn't sprawl.
        return "month"
    span_seconds = (end - start).total_seconds()
    span_minutes = span_seconds / 60
    # Target ~120 buckets per chart for readable resolution.
    if span_minutes <= 60:                  # up to 1h
        return "minute"
    if span_minutes <= 6 * 60:              # up to 6h
        return "5min"
    if span_minutes <= 24 * 60:             # up to 24h
        return "15min"
    if span_minutes <= 7 * 24 * 60:         # up to 1w
        return "hour"
    if span_minutes <= 60 * 24 * 60:        # up to 60d
        return "day"
    if span_minutes <= 365 * 24 * 60:       # up to ~1y
        return "week"
    return "month"


def _bucket_start_rate(dt: datetime, interval: str) -> datetime:
    """Floor a datetime to the start of its bucket for the rate
    chart. Minute-level intervals quantize to the nearest sub-hour
    cell (5-min and 15-min buckets start at multiples of 5/15
    minutes within the hour); hour/day/week reuse the calendar
    logic from the throughput chart's helper."""
    dt = dt.astimezone(timezone.utc)
    if interval == "minute":
        return dt.replace(second=0, microsecond=0)
    if interval == "5min":
        m = dt.minute - (dt.minute % 5)
        return dt.replace(minute=m, second=0, microsecond=0)
    if interval == "15min":
        m = dt.minute - (dt.minute % 15)
        return dt.replace(minute=m, second=0, microsecond=0)
    if interval == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    if interval == "day":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if interval == "week":
        d = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return d - timedelta(days=d.weekday())
    # month
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_bucket_rate(dt: datetime, interval: str) -> datetime:
    """Advance to the next bucket's start for the rate chart."""
    if interval in ("minute", "5min", "15min", "hour", "day", "week"):
        return dt + timedelta(seconds=_RATE_INTERVAL_SECONDS[interval])
    # month — handle year rollover.
    if dt.month == 12:
        return dt.replace(year=dt.year + 1, month=1)
    return dt.replace(month=dt.month + 1)


class SubmissionRateBucket(BaseModel):
    """One time bucket on the submission-rate line. `start` is the
    bucket boundary (ISO datetime); `count` is the number of
    submissions whose `created_at` falls into the bucket. Unlike
    throughput, no state breakdown — for activity/security
    monitoring the answer to "did volume spike?" should not be
    distributed across a stacked color encoding."""
    start: str
    count: int


class SubmissionRateResponse(BaseModel):
    buckets: list[SubmissionRateBucket]
    interval: Literal["minute", "5min", "15min", "hour", "day", "week", "month"]
    total: int
    # Summary stats over the bucket population — `peak_count` is the
    # tallest bucket (the spike), `peak_start` is when it occurred.
    # `mean_count` is the average across buckets in the filled range
    # (including zero-count gaps), useful as an implicit baseline
    # without an explicit baseline overlay.
    peak_count: int = 0
    peak_start: Optional[str] = None
    mean_count: float = 0.0
    filters_applied: dict[str, Any]


@api.get(
    "/forms/{form_id}/analytics/submission_rate",
    response_model=SubmissionRateResponse,
    dependencies=[Depends(require_form_access("view"))],
)
def analytics_submission_rate(
    form_id: str,
    state: Optional[list[str]] = Query(default=None),
    current_step: Optional[list[str]] = Query(default=None),
    date_range: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    interval: Optional[str] = None,
) -> SubmissionRateResponse:
    """Submissions per fine-grained time bucket. Same x-axis as the
    throughput chart but at minute-level resolution and without
    state stacking — designed for spotting volume spikes (attack
    traffic, viral inbound, etc.). The interval auto-picks from the
    date range across `minute`/`5min`/`15min`/`hour`/`day`/`week`/
    `month` unless overridden.
    """
    form = _load_form_or_404(form_id)
    filters = _parse_analytics_filters(
        form,
        state=state, current_step=current_step,
        date_range=date_range,
        start_date=start_date, end_date=end_date,
    )
    chosen_interval = _resolve_rate_interval(interval, filters)
    rows = _filter_submissions(
        store.list_all_form_submissions(form_id), filters,
    )
    if not rows:
        return SubmissionRateResponse(
            buckets=[], interval=chosen_interval, total=0,
            filters_applied=_filters_for_response(filters),
        )
    # Single dimension — count per bucket.
    by_bucket: dict[datetime, int] = {}
    for r in rows:
        ca = r.get("created_at")
        if not ca:
            continue
        b = _bucket_start_rate(ca, chosen_interval)
        by_bucket[b] = by_bucket.get(b, 0) + 1
    # Fill empty buckets between earliest and latest so a zero-rate
    # period reads as a flat line dropping to zero, not a missing
    # gap — critical for spike detection (a quiet hour next to a
    # spike makes the spike visually obvious).
    if by_bucket:
        cursor = min(by_bucket.keys())
        end = max(by_bucket.keys())
        while cursor < end:
            by_bucket.setdefault(cursor, 0)
            cursor = _next_bucket_rate(cursor, chosen_interval)
    sorted_items = sorted(by_bucket.items())
    out = [
        SubmissionRateBucket(start=b.isoformat(), count=c)
        for b, c in sorted_items
    ]
    counts = [c for _, c in sorted_items]
    peak_idx = max(range(len(counts)), key=lambda i: counts[i])
    return SubmissionRateResponse(
        buckets=out,
        interval=chosen_interval,
        total=sum(counts),
        peak_count=counts[peak_idx],
        peak_start=sorted_items[peak_idx][0].isoformat(),
        mean_count=sum(counts) / len(counts),
        filters_applied=_filters_for_response(filters),
    )


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
    # Every chain-step's return value, keyed by producer name (an
    # `@backend` function's name or an Airflow operator's task id).
    # Nullable for steps with no chain outputs (form pages with no
    # backend or external tasks). The submission-detail UI renders
    # one row per entry so multi-backend nodes show *all* outputs,
    # not just the legacy `backend_return` singleton (which holds
    # only the first backend's value).
    chain_outputs: Optional[dict[str, Any]] = None
    button_clicked: Optional[str] = None
    # Display labels for picker-field values (Phase 4 — replaces
    # bare identifiers like user_id=1 with the resolved label like
    # "admin" in the submission summary). Shape:
    #   {field_name: {identifier: label}}
    # The frontend renders `label (identifier)` so the underlying
    # value is still visible for power users / debugging. Null when
    # no picker fields are in this step.
    value_labels: Optional[dict[str, dict[str, str]]] = None
    # Identifier kind per picker field (so the frontend can link
    # user-id values to the /users page). Shape:
    #   {field_name: "frontflow_user_id" | "external_id" | ...}
    # Null when no picker fields are in this step.
    value_kinds: Optional[dict[str, str]] = None
    # Per-step error message. Set when a backend/chain step raised;
    # `state` is "failed" in that case. Surfaces in the chain UI so
    # the user sees the actual exception (e.g. "ValueError: cannot
    # convert float NaN to integer") instead of just "failed".
    error: Optional[str] = None
    # Full Python traceback paired with `error`. Shown in the chain
    # UI's collapsible details panel under the short message.
    traceback: Optional[str] = None
    # Children spawned from this step by an Assign operator on the
    # corresponding node. Empty for steps that didn't fire an
    # Assign. Drives the parent submission's graph + step block
    # rendering of inline child chips — surfaces the parent-child
    # relationship in the admin summary, not just in /my-tasks
    # for the assignee.
    assignments: list["AssignedChild"] = []


class EventRow(BaseModel):
    type: str
    node_id: Optional[str] = None
    page_id: Optional[str] = None
    occurred_at: datetime
    payload: Optional[dict[str, Any]] = None


class VersionOption(BaseModel):
    """One entry in a submission's version picker — a form version this
    submission has data on, with the human-facing integer (major +
    minor) and whether it is the currently-active version."""
    id: int
    version: int
    minor_version: int = 0
    is_active: bool


class ChildGraph(BaseModel):
    """One spawned child submission's graph + state, attached to the
    parent submission's response for the nested-graph viz.

    The parent's `/detail` endpoint walks every assignment granted by
    this submission (and recursively, by its children), producing one
    `ChildGraph` per distinct child submission encountered. Each
    carries:
      - parent provenance: which parent submission + node spawned us
      - the child form's static graph (same shape as /forms/{id}/graph)
      - per-node run state for THIS child submission
      - metadata for rendering (role, assignee, revoked-state)

    Recursion is depth-limited (default 10) with cycle detection so a
    misconfigured form that Assigns back to an ancestor doesn't blow
    the response.
    """
    parent_submission_handle: str
    parent_node_id: str
    assignment_id: int
    child_form_id: str
    child_form_title: str
    child_submission_handle: str
    child_submission_id: Optional[str] = None
    child_submission_state: str
    role_id: str
    assignee_user_id: int
    assignee_username: Optional[str] = None
    granted_at: datetime
    revoked_at: Optional[datetime] = None
    # The child form's static graph — same structure as the
    # `/forms/{id}/graph` endpoint. The frontend reuses its existing
    # graph renderer with this payload.
    graph: WorkflowGraph
    # Per-node run state for this specific child submission. Keys
    # are node ids in `graph`; values are "succeeded" / "running" /
    # "failed" / "not_reached" (matching the parent's nodeState
    # convention).
    node_state: dict[str, str]
    # Depth in the spawn tree — 1 = direct child of the rendered
    # submission, 2 = grandchild, etc. Caps at `_CHILD_GRAPH_MAX_DEPTH`
    # to prevent runaway recursion.
    depth: int


_CHILD_GRAPH_MAX_DEPTH = 10


def _build_child_graphs(
    root_handle: str,
) -> list[ChildGraph]:
    """Recursively walk every child submission spawned (transitively)
    by the root submission. Returns one entry per child submission,
    ordered by depth then granted_at.

    Cycle detection: a child submission whose handle is already on
    the walk stack is skipped — handles a misconfigured form that
    Assigns back to an ancestor. Depth cap stops a long-but-not-
    cyclic chain from rendering to infinity.

    Pulls all DB data up-front (one query per kind, no N+1).
    Resolves each child form's graph from the live FORMS registry
    — if the form is no longer deployed in this process, the entry
    is skipped silently (rare edge case; flagged for follow-up).
    """
    from frontflow.dsl.store import (
        SubmissionAssignment as _Assignment,
        Submission as _SubmissionRow,
        User as _User,
        Form as _FormRow,
        FormVersion as _FormVersionRow,
        _engine,
    )
    from sqlalchemy import select as _select
    from sqlalchemy.orm import Session as _DBSession

    out: list[ChildGraph] = []
    # Visit order: BFS by depth. Each iteration processes one level
    # of the tree; the next level's parent handles come from this
    # level's child handles.
    visited: set[str] = {root_handle}
    current_level: list[str] = [root_handle]
    depth = 0

    while current_level and depth < _CHILD_GRAPH_MAX_DEPTH:
        depth += 1
        next_level: list[str] = []

        with _DBSession(_engine) as db:
            # All assignments granted at this level.
            rows = db.execute(
                _select(_Assignment).where(
                    _Assignment.granted_by_submission_handle.in_(
                        current_level,
                    ),
                ).order_by(_Assignment.granted_at)
            ).scalars().all()
            if not rows:
                break

            # Bulk-fetch the child submissions referenced.
            child_handles = [r.submission_handle for r in rows]
            sub_rows = db.execute(
                _select(_SubmissionRow).where(
                    _SubmissionRow.handle.in_(child_handles),
                )
            ).scalars().all()
            sub_by_handle = {s.handle: s for s in sub_rows}

            # Resolve form ids + titles via the form_version table.
            version_ids = list({
                s.form_version_id for s in sub_rows
                if s.form_version_id is not None
            })
            version_to_form_id: dict[int, str] = {}
            if version_ids:
                for fv in db.execute(
                    _select(_FormVersionRow).where(
                        _FormVersionRow.id.in_(version_ids),
                    )
                ).scalars().all():
                    version_to_form_id[fv.id] = fv.form_id
            form_ids = set(version_to_form_id.values())
            form_titles: dict[str, str] = {}
            if form_ids:
                for fr in db.execute(
                    _select(_FormRow).where(
                        _FormRow.form_id.in_(form_ids),
                    )
                ).scalars().all():
                    form_titles[fr.form_id] = fr.name or fr.form_id

            # Assignee usernames.
            user_ids = {r.user_id for r in rows}
            usernames: dict[int, str] = {}
            if user_ids:
                for u in db.execute(
                    _select(_User).where(_User.id.in_(user_ids))
                ).scalars().all():
                    usernames[u.id] = u.username

            # Per-child step rows (for the node_state map). Bulk
            # fetch all steps for every child submission in this
            # level, then partition.
            from frontflow.dsl.store import Step as _StepRow
            step_rows = db.execute(
                _select(_StepRow).where(
                    _StepRow.submission_handle.in_(child_handles),
                )
            ).scalars().all()
            steps_by_handle: dict[str, list[_StepRow]] = {}
            for st in step_rows:
                steps_by_handle.setdefault(
                    st.submission_handle, [],
                ).append(st)

        for r in rows:
            sub = sub_by_handle.get(r.submission_handle)
            if sub is None:
                continue
            if sub.handle in visited:
                # Cycle — already processed this submission at a
                # shallower depth. Skip without recursing.
                continue
            visited.add(sub.handle)
            child_form_id = (
                version_to_form_id.get(sub.form_version_id)
                if sub.form_version_id is not None
                else ""
            ) or ""
            # Resolve the child's static graph from the live form
            # registry. If the form isn't deployed in this process
            # (e.g. it was renamed/removed), skip — we can't
            # render its structure. The user-detail page still
            # surfaces the assignment.
            cw = FORMS.get(child_form_id)
            if cw is None:
                continue
            try:
                child_graph = _build_workflow_graph(cw)
            except Exception as e:  # noqa: BLE001 — defensive
                # A compile-time graph builder failure shouldn't
                # break the parent's submission view.
                print(
                    f"[child_graphs] WARNING: building graph for "
                    f"{child_form_id!r} failed: {e}"
                )
                continue
            # Per-node run state for this child. Step rows track
            # submission timestamp; the submission-level `error`
            # column flags overall failure. Per-step state is
            # derived from each step's submitted_at, with the
            # latest unsubmitted step on a failed submission
            # marked failed (mirrors SubmissionGraph's frontend
            # mapping).
            node_state: dict[str, str] = {}
            sub_failed = (sub.state or "") == "failed"
            child_steps = steps_by_handle.get(sub.handle, [])
            # Sort by seq so we know the "latest" step for failure
            # attribution.
            child_steps.sort(key=lambda st: getattr(st, "seq", 0))
            for i, st in enumerate(child_steps):
                if st.submitted_at is not None:
                    node_state[st.node_id] = "succeeded"
                elif sub_failed and i == len(child_steps) - 1:
                    node_state[st.node_id] = "failed"
                else:
                    node_state[st.node_id] = "running"
            # Nodes the child hasn't reached yet stay absent from
            # the map — the frontend defaults them to "not_reached".

            out.append(ChildGraph(
                parent_submission_handle=(
                    sub.parent_submission_handle or root_handle
                ),
                parent_node_id=sub.parent_assign_node_id or "",
                assignment_id=r.id,
                child_form_id=child_form_id,
                child_form_title=form_titles.get(
                    child_form_id, child_form_id,
                ),
                child_submission_handle=sub.handle,
                child_submission_id=sub.submission_id,
                child_submission_state=sub.state or "unknown",
                role_id=r.role_id,
                assignee_user_id=r.user_id,
                assignee_username=usernames.get(r.user_id),
                granted_at=r.granted_at,
                revoked_at=r.revoked_at,
                graph=child_graph,
                node_state=node_state,
                depth=depth,
            ))
            # Queue this child's handle for the next BFS level —
            # its own Assigns produce grandchildren.
            next_level.append(sub.handle)

        current_level = next_level

    return out


class SubmissionDetail(BaseModel):
    submission_id: Optional[str] = None
    handle: str
    form_id: str
    state: str
    # The form version (human-facing major integer) this submission ran on.
    form_version: int
    # The form's *current* live version (major). When this is greater
    # than `form_version` the submission is pinned to an older
    # structural revision — admins may re-pin via POST /repin.
    live_form_version: int
    # Minor-version counterparts. Zero when the form has never had a
    # non-structural revision since the major being viewed. Rendered
    # as the `.N` suffix on the version chip; suppressed when 0.
    form_minor_version: int = 0
    live_minor_version: int = 0
    # The version *being viewed*. Equals `form_version` for the active
    # chain; when a frozen version is requested via `?version=<id>`,
    # this is that historical version. The frontend uses this to
    # render the "viewing read-only history" banner.
    viewing_version: int
    viewing_version_id: int
    viewing_minor_version: int = 0
    is_viewing_active: bool
    # Every form_version this submission has data on, oldest first —
    # powers the picker on the submission summary page.
    available_versions: list[VersionOption]
    created_at: datetime
    terminated_at: Optional[datetime] = None
    error: Optional[str] = None
    steps: list[StepDetailRow]
    events: list[EventRow]
    # Spawned child submissions, recursively walked from this
    # submission. Empty when no Assigns fired. Each entry carries
    # the child form's static graph plus per-node state for that
    # specific child submission — enabling the parent's graph view
    # to render nested child clusters. See `_build_child_graphs`
    # for the walk semantics (BFS by depth, cycle-guarded, capped
    # at _CHILD_GRAPH_MAX_DEPTH).
    child_graphs: list[ChildGraph] = []
    # When the submission's pinned form_version source no longer
    # compiles, the runtime falls back to deserializing the stored
    # compiled_graph. This makes the submission viewable but blocks
    # any advance/submit operation (no executable backends). The
    # field carries the original compile error so the UI can surface
    # a banner explaining the state. None when source compiled
    # normally — the common case.
    form_version_compile_error: Optional[str] = None


@api.get(
    "/forms/{form_id}/submissions/{submission_id}/detail",
    response_model=SubmissionDetail,
)
def read_submission_detail(
    form_id: str, submission_id: str, version: int | None = None,
    frontflow_session: str | None = Cookie(default=None),
    token: str | None = None,
) -> SubmissionDetail:
    """The persisted record of one submission — every step with the
    data it captured, plus the append-only event history. Powers the
    submission summary page; distinct from the runtime DAG view at
    `GET /forms/{id}/submissions/{id}`.

    `version` (query) picks which form_version's chain to render.
    Omit to view the active chain (today's behavior). Provide a
    form_version_id from `available_versions` to view a frozen
    historical chain — steps and events are scoped to that version,
    and titles/labels resolve against that version's compiled form
    so the data reads under the schema it was captured under.

    Visibility is gated per submission, not per form: callers must
    be admins, folder-grant holders, step submitters, active
    assignees, or grantors on this submission. Valid signed-link
    tokens also bypass. Non-permitted callers get 404.
    """
    form, submission = _get_submission_or_404(form_id, submission_id)
    if not _token_bears_submission(token, submission.handle):
        user = auth.resolve_session(frontflow_session)
        require_submission_visibility(form, submission, user)
    # When the pinned form source no longer compiles, skip `advance`
    # entirely — it would try to invoke placeholder backends and
    # raise. The submission is viewable from its recorded data; no
    # forward progress is attempted. `_persist` is also skipped (no
    # state changed; nothing to write).
    if not getattr(form, "source_unavailable", False):
        advance(form, submission)
        _persist(form, submission)

    snap = submission_snapshot(form, submission)
    fv = store.get_form_version(snap["form_version_id"])
    active_version_num = fv["version"] if fv is not None else 0
    active_minor_num = fv["minor_version"] if fv is not None else 0

    # Translate the form's live form_version_id to its human-facing
    # integer the same way. When the form has been deleted from disk,
    # there's no live version — fall back to the submission's version.
    live_version_id = FORM_VERSION_IDS.get(form.id)
    if live_version_id is None:
        live_version = active_version_num
        live_minor = active_minor_num
    else:
        live_fv = store.get_form_version(live_version_id)
        if live_fv is not None:
            live_version = live_fv["version"]
            live_minor = live_fv["minor_version"]
        else:
            live_version = active_version_num
            live_minor = active_minor_num

    # Build the version picker — every version this submission has
    # data on, with the active-flag set on the current one.
    available = store.list_submission_versions(submission.handle)
    available_versions = [
        VersionOption(
            id=v["id"],
            version=v["version"],
            minor_version=v["minor_version"],
            is_active=(v["id"] == snap["form_version_id"]),
        )
        for v in available
    ]

    # Resolve which version's chain to render. Default = active.
    if version is None:
        viewing_version_id = snap["form_version_id"]
    else:
        viewing_version_id = version

    # Validate: requested version must be one this submission has data
    # on — refusing arbitrary ids prevents leaking other submissions'
    # form versions through this endpoint.
    if viewing_version_id not in {v["id"] for v in available}:
        raise HTTPException(
            status_code=404,
            detail=(
                f"this submission has no data on form_version_id "
                f"{viewing_version_id}"
            ),
        )

    is_active_view = viewing_version_id == snap["form_version_id"]

    # Pick the workflow used to resolve titles/labels for the chain
    # being rendered. The active chain uses `form` (the live or pinned
    # workflow already resolved). A frozen chain needs the *historical*
    # workflow — `resolve_workflow` rebuilds it from the stored source.
    if is_active_view:
        view_form = form
        chain_steps = snap["steps"]
        chain_events = snap["events"]
    else:
        try:
            view_form = resolve_workflow(viewing_version_id)
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"form_version {viewing_version_id} is no longer "
                    "in the version store — its history cannot be "
                    "reconstructed"
                ),
            )
        frozen = store.load_submission_frozen_chain(
            submission.handle, viewing_version_id,
        )
        chain_steps = frozen["steps"]
        chain_events = frozen["events"]

    viewing_fv = store.get_form_version(viewing_version_id)
    viewing_version_num = (
        viewing_fv["version"] if viewing_fv is not None else 0
    )
    viewing_minor_num = (
        viewing_fv["minor_version"] if viewing_fv is not None else 0
    )

    steps: list[StepDetailRow] = []
    # Bulk-fetch every assignment granted by THIS submission so each
    # step block can show its spawned children inline. Walks the
    # SubmissionAssignment + child Submission tables; cheap (~4
    # queries for an entire submission render regardless of step
    # count). The empty-list default on the model keeps the
    # response shape backward-compatible — old frontends ignore
    # the new field.
    assignments_by_node = _load_assignments_granted_by(snap["handle"])

    for s in chain_steps:
        # Resolve title against the *viewed* workflow so frozen views
        # render under v(viewed)'s labels, not v(active)'s.
        node = view_form.all_nodes_by_id.get(s["node_id"])
        # Project external_state into a flat dict of producer → return
        # value. Skip entries with no return (in-flight tasks the user
        # could see mid-poll). Order matters for UX: render in the
        # original chain declaration order if we have it; otherwise
        # in dict insertion order (Python 3.7+ preserves it).
        ext = s.get("external_state") or {}
        chain_outputs: Optional[dict[str, Any]] = None
        if ext:
            out: dict[str, Any] = {}
            for k, v in ext.items():
                if isinstance(v, dict) and "return" in v:
                    out[k] = v["return"]
            chain_outputs = out or None
        # Picker-field labels: for any picker field in this step,
        # resolve its current value(s) to display labels so the
        # submission UI renders "admin" instead of "1". The kinds
        # map lets the frontend link only user-id values to the
        # /users page. See _resolve_step_picker_labels.
        picker_labels: Optional[dict[str, dict[str, str]]] = None
        picker_kinds: Optional[dict[str, str]] = None
        if node is not None and s["form_values"]:
            picker_labels, picker_kinds = _resolve_step_picker_labels(
                node, s["form_values"]
            )

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
                chain_outputs=chain_outputs,
                button_clicked=s["button_clicked"],
                value_labels=picker_labels,
                value_kinds=picker_kinds,
                assignments=assignments_by_node.get(s["node_id"], []),
                error=s.get("error"),
                traceback=s.get("traceback"),
            )
        )

    return SubmissionDetail(
        submission_id=snap["submission_id"],
        handle=snap["handle"],
        form_id=form.id,
        state=snap["state"],
        form_version=active_version_num,
        live_form_version=live_version,
        form_minor_version=active_minor_num,
        live_minor_version=live_minor,
        viewing_version=viewing_version_num,
        viewing_version_id=viewing_version_id,
        viewing_minor_version=viewing_minor_num,
        is_viewing_active=is_active_view,
        available_versions=available_versions,
        created_at=snap["created_at"],
        terminated_at=snap["terminated_at"],
        error=snap["error"],
        steps=steps,
        events=[EventRow(**e) for e in chain_events],
        child_graphs=_build_child_graphs(snap["handle"]),
        # Surface the compile error from the workflow being viewed.
        # When source compiled normally, getattr returns False and
        # the field stays None. When the deserialize fallback fired,
        # `source_unavailable` is True and `source_error` carries
        # the original ImportError / SyntaxError / etc.
        form_version_compile_error=(
            getattr(form, "source_error", None) or None
            if getattr(form, "source_unavailable", False)
            else None
        ),
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


class _SoftDeleteSubmissionsRequest(BaseModel):
    """Body for `POST /forms/{form_id}/submissions/delete`. The
    listing UI knows each row by its `handle` (a stable string the
    runtime mints early in the submission's life); minted
    `submission_id`s aren't usable because drafts may not have one
    yet."""
    handles: list[str] = Field(default_factory=list)


class _SoftDeleteSubmissionsResponse(BaseModel):
    """Response for the soft-delete endpoint. Returns per-bucket
    handles so the frontend can render an accurate completion toast
    ("N deleted, M not found") rather than a single all-or-nothing
    flag — partial outcomes are normal when a stale listing in one
    tab is acted on after another tab has already deleted some of
    the same rows."""
    deleted: list[str]
    not_found: list[str]


@api.post(
    "/forms/{form_id}/submissions/delete",
    response_model=_SoftDeleteSubmissionsResponse,
)
def soft_delete_submissions(
    form_id: str,
    body: _SoftDeleteSubmissionsRequest,
    _admin: "store.User" = Depends(require_admin),
) -> _SoftDeleteSubmissionsResponse:
    """Soft-delete the named submissions. Sets `deleted_at` on each
    row rather than physically removing it — Step / Event /
    SubmissionBlob history stays intact for a future undelete.

    Admin-only. Scoped to `form_id` so a handle from form A can't
    be tombstoned by passing it to form B's URL (defense-in-depth
    on top of the admin gate). Handles that don't resolve to a
    live submission for THIS form land in `not_found` rather than
    surfacing as an error — partial outcomes are normal when two
    admins delete from the same stale listing.

    Side effects beyond the DB write:
      - In-memory `runtime._submissions` and `runtime._id_index`
        are pruned for the tombstoned rows. Without this, the
        cached entry would keep advancing the submission and the
        cached id would block any future re-use.
    """
    deleted_pairs, not_found = store.soft_delete_submissions_by_handles(
        body.handles, form_id=form_id,
    )

    if deleted_pairs:
        # Evict from the runtime's in-memory maps. The lock is the
        # same one `start_submission` / `hydrate_submission` use, so
        # we can't have a hydrate racing this in between the DB
        # write and the cache prune.
        with runtime._submissions_lock:
            for handle, submission_id in deleted_pairs:
                runtime._submissions.pop(handle, None)
                if submission_id is not None:
                    # Only drop the id-index entry if it still points
                    # at THIS handle. A future `start_submission`
                    # that re-mints the same id (unlikely, but
                    # possible for forms with deterministic
                    # `submission_id` templates) would have rewritten
                    # the index already; we don't want to undo that.
                    if runtime._id_index.get(submission_id) == handle:
                        runtime._id_index.pop(submission_id, None)

    return _SoftDeleteSubmissionsResponse(
        deleted=[h for h, _ in deleted_pairs],
        not_found=not_found,
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
    versions: str = "active",
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

    if versions not in ("active", "all"):
        raise HTTPException(
            status_code=422,
            detail="versions must be 'active' or 'all'",
        )

    try:
        return store.export_submissions(
            limit=limit,
            cursor=cursor,
            updated_since=since_dt,
            updated_before=before_dt,
            form_id=form_id,
            terminal_only=terminal_only,
            versions=versions,
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


class UserAssignmentRow(BaseModel):
    """One row in the per-user assignment listing — every active
    or historically-revoked SubmissionAssignment for this user.
    Drives the admin user detail page's Access tab.
    """
    assignment_id: int
    submission_handle: str
    submission_id: Optional[str]
    submission_state: str
    form_id: str
    form_title: str
    role_id: str
    granted_at: datetime
    granted_by_user_id: int
    granted_by_username: Optional[str]
    revoked_at: Optional[datetime] = None
    revoked_by_user_id: Optional[int] = None
    revoked_by_username: Optional[str] = None


@api.get(
    "/users/{user_id}/assignments",
    response_model=list[UserAssignmentRow],
    dependencies=[Depends(require_admin)],
)
def list_user_assignments(
    user_id: int, include_revoked: bool = True,
) -> list[UserAssignmentRow]:
    """Every assignment ever granted to or revoked from this user.

    Drives the admin user-detail page's Access tab. Returns active
    rows by default plus historical ones; pass `include_revoked=false`
    to filter to active-only. Joined with the parent submission to
    surface form_id/title + submission state for context, and with
    the User table to resolve granter/revoker usernames.

    Admin-only (this lists data about a user that the user
    themselves wouldn't normally see in this aggregated form).
    """
    from frontflow.dsl.store import (
        SubmissionAssignment as _Assignment,
        Submission as _SubmissionRow,
        User as _User,
        Form as _FormRow,
        FormVersion as _FormVersionRow,
        _engine,
    )
    from sqlalchemy import select as _select
    from sqlalchemy.orm import Session as _DBSession

    with _DBSession(_engine) as db:
        q = _select(_Assignment).where(_Assignment.user_id == user_id)
        if not include_revoked:
            q = q.where(_Assignment.revoked_at.is_(None))
        q = q.order_by(_Assignment.granted_at.desc())
        rows = db.execute(q).scalars().all()
        if not rows:
            return []

        # Bulk-fetch related entities in one query each to avoid
        # N+1. Submission → form_version → form for title resolution.
        handles = [r.submission_handle for r in rows]
        sub_rows = db.execute(
            _select(_SubmissionRow).where(
                _SubmissionRow.handle.in_(handles),
            )
        ).scalars().all()
        sub_by_handle = {s.handle: s for s in sub_rows}

        form_version_ids = list({
            s.form_version_id for s in sub_rows
            if s.form_version_id is not None
        })
        form_ids: set[str] = set()
        version_to_form: dict[int, str] = {}
        if form_version_ids:
            for fv in db.execute(
                _select(_FormVersionRow).where(
                    _FormVersionRow.id.in_(form_version_ids),
                )
            ).scalars().all():
                version_to_form[fv.id] = fv.form_id
                form_ids.add(fv.form_id)

        form_titles: dict[str, str] = {}
        if form_ids:
            for fr in db.execute(
                _select(_FormRow).where(_FormRow.form_id.in_(form_ids))
            ).scalars().all():
                form_titles[fr.form_id] = fr.name or fr.form_id

        # User usernames for granter + revoker columns.
        user_ids: set[int] = set()
        for r in rows:
            user_ids.add(r.granted_by_user_id)
            if r.revoked_by_user_id is not None:
                user_ids.add(r.revoked_by_user_id)
        usernames: dict[int, str] = {}
        if user_ids:
            for u in db.execute(
                _select(_User).where(_User.id.in_(user_ids))
            ).scalars().all():
                usernames[u.id] = u.username

    out: list[UserAssignmentRow] = []
    for r in rows:
        sub = sub_by_handle.get(r.submission_handle)
        form_id = (
            version_to_form.get(sub.form_version_id)
            if sub and sub.form_version_id is not None
            else ""
        ) or ""
        out.append(UserAssignmentRow(
            assignment_id=r.id,
            submission_handle=r.submission_handle,
            submission_id=sub.submission_id if sub else None,
            submission_state=(sub.state if sub else "unknown"),
            form_id=form_id,
            form_title=form_titles.get(form_id, form_id),
            role_id=r.role_id,
            granted_at=r.granted_at,
            granted_by_user_id=r.granted_by_user_id,
            granted_by_username=usernames.get(r.granted_by_user_id),
            revoked_at=r.revoked_at,
            revoked_by_user_id=r.revoked_by_user_id,
            revoked_by_username=(
                usernames.get(r.revoked_by_user_id)
                if r.revoked_by_user_id is not None
                else None
            ),
        ))
    return out


@api.post(
    "/assignments/{assignment_id}/revoke",
    dependencies=[Depends(require_admin)],
)
def revoke_assignment_endpoint(
    assignment_id: int,
    frontflow_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    """Revoke a single assignment by id. Returns the updated row;
    `revoked_at` is set, the on_revoked hook fires. Already-revoked
    rows return 404 (no-op rather than double-revoke).

    Admin-only — assignment management is a privileged operation.
    """
    actor = auth.resolve_session(frontflow_session)
    if actor is None:
        # require_admin already covers this, but be defensive.
        raise HTTPException(status_code=401, detail="not signed in")
    from frontflow.dsl import assignments as _assignments
    row = _assignments.revoke(
        assignment_id=assignment_id,
        revoked_by_user_id=actor.id,
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="assignment not found or already revoked",
        )
    return {
        "assignment_id": row.id,
        "submission_handle": row.submission_handle,
        "user_id": row.user_id,
        "role_id": row.role_id,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "revoked_by_user_id": row.revoked_by_user_id,
    }


@api.post(
    "/submissions/{submission_handle}/users/{user_id}/revoke-all",
    dependencies=[Depends(require_admin)],
)
def revoke_all_user_assignments_on_submission_endpoint(
    submission_handle: str,
    user_id: int,
    frontflow_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    """Revoke every active assignment a user holds on one submission.

    The motivating case: an admin reviewing a user's access page sees
    several active roles on the same submission (e.g. `approver` and
    `signer`) and wants to cut all of them in one click rather than
    revoking each row individually. Mirrors the per-row endpoint's
    audit semantics — each row is revoked individually, `on_revoked`
    fires once per row, the actor on every row is the calling admin.

    Returns `{"revoked_count": N}`. N may be zero when the user has
    no active grants on that submission (already revoked / never
    granted); the response is 200, not 404 — bulk operations should
    be idempotent. Confirming the user/submission exist would be
    nice but neither id is leak-sensitive (the admin already saw
    both on the user-detail page), and the revoke is a no-op if they
    don't.

    Admin-only — assignment management is privileged.
    """
    actor = auth.resolve_session(frontflow_session)
    if actor is None:
        # require_admin already covers this, but be defensive.
        raise HTTPException(status_code=401, detail="not signed in")
    from frontflow.dsl import assignments as _assignments
    count = _assignments.revoke_all_for_user_on_submission(
        submission_handle=submission_handle,
        user_id=user_id,
        revoked_by_user_id=actor.id,
    )
    return {"revoked_count": count}


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


# --- External-identity endpoints -------------------------------------------
#
# Endpoints intended for the customer's identity system (LMS, HR
# system, SSO IdP) to call when a user is deactivated, renamed, or
# needs to be reconciled. Authentication is admin-only — these are
# privileged operations and should be called via a service-account
# session or admin token.

from frontflow.dsl import external_identity as _ext_id  # noqa: E402


class ExternalUserUpdate(BaseModel):
    username: Optional[str] = None
    email: Optional[str] = None


@api.get(
    "/users/external/{external_id}",
    dependencies=[Depends(require_admin)],
)
def get_external_user(external_id: str) -> dict[str, Any]:
    """Look up a user by external_id.

    Does NOT call the resolver hook — returns 404 if no row exists
    locally. Use this from external systems to verify the mapping
    is current (the resolver hook is for the request-time path
    where frontflow encounters an unknown external_id mid-flow)."""
    user = _ext_id.lookup(external_id)
    if user is None:
        raise HTTPException(
            status_code=404,
            detail=f"no user with external_id {external_id!r}",
        )
    return {
        "id": user.id,
        "username": user.username,
        "external_id": user.external_id,
        "is_admin": user.is_admin,
        "is_active": user.is_active,
    }


@api.put(
    "/users/external/{external_id}",
    dependencies=[Depends(require_admin)],
)
def update_external_user(
    external_id: str, req: ExternalUserUpdate
) -> dict[str, Any]:
    """Update mapped attributes on an external user.

    The `external_id` itself is NOT editable here — changing it
    would silently misroute every existing assignment and signed
    link. To change the external mapping, deactivate the old row
    and create a new one with the new external_id."""
    user = _ext_id.update(
        external_id,
        username=req.username,
        email=req.email,
    )
    if user is None:
        raise HTTPException(
            status_code=404,
            detail=f"no user with external_id {external_id!r}",
        )
    return {
        "id": user.id,
        "username": user.username,
        "external_id": user.external_id,
        "is_active": user.is_active,
    }


@api.delete(
    "/users/external/{external_id}",
)
def deactivate_external_user(
    external_id: str,
    session_user: "store.User" = Depends(require_admin),
) -> dict[str, bool]:
    """Mark an external user inactive.

    Preserves the User row and its audit history but blocks future
    authentication. When Phase 4 lands, this also revokes every
    active submission_assignment for the user — for Phase 2, only
    the deactivation step runs. The row is recoverable: reactivate
    via the admin user-management endpoint."""
    ok = _ext_id.deactivate(
        external_id,
        by_user_id=session_user.id if session_user else None,
    )
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"no user with external_id {external_id!r}",
        )
    return {"ok": True}


# --- Per-user notification preferences -------------------------------------


class NotificationPrefsUpdate(BaseModel):
    preferences: dict[str, bool]


@api.get(
    "/users/me/notification-preferences",
    response_model=dict,
)
def get_my_notification_preferences(
    session_user: "store.User" = Depends(_current_user),
) -> dict[str, bool]:
    """Returns the signed-in user's notification preferences."""
    return dict(session_user.notification_preferences or {})


@api.put(
    "/users/me/notification-preferences",
    response_model=dict,
)
def update_my_notification_preferences(
    req: NotificationPrefsUpdate,
    session_user: "store.User" = Depends(_current_user),
) -> dict[str, bool]:
    """Update the signed-in user's notification preferences.
    Overwrites the whole dict — pass the complete desired state."""
    return auth.set_notification_preferences(
        session_user.id, dict(req.preferences)
    )


@api.get(
    "/users/{user_id}/notification-preferences",
    dependencies=[Depends(require_admin)],
)
def get_user_notification_preferences(
    user_id: int,
) -> dict[str, bool]:
    """Admin view of any user's notification preferences."""
    prefs = auth.get_notification_preferences(user_id)
    if prefs is None:
        raise HTTPException(
            status_code=404, detail=f"no user with id {user_id}"
        )
    return prefs


@api.put(
    "/users/{user_id}/notification-preferences",
    dependencies=[Depends(require_admin)],
)
def admin_update_notification_preferences(
    user_id: int, req: NotificationPrefsUpdate
) -> dict[str, bool]:
    """Admin overwrites any user's notification preferences."""
    try:
        return auth.set_notification_preferences(
            user_id, dict(req.preferences)
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# --- My-tasks inbox --------------------------------------------------------
#
# The default in-app inbox — every active assignment for the signed-in
# user, across forms. The floor per design doc P5; not gated by
# notification preferences.

from frontflow.dsl import assignments as _assignments  # noqa: E402
from frontflow.dsl import store as _store_module  # noqa: E402


@api.get("/my-tasks")
def my_tasks(
    session_user: "store.User" = Depends(_current_user),
) -> list[dict[str, Any]]:
    """Every active assignment for the signed-in user.

    Returns a list ordered by `granted_at` desc — newest first.
    Each entry carries enough to render an inbox row: assignment id,
    submission handle + state, form id + title, granted_at, role.

    Filter and search are V2; this is the floor.
    """
    return _my_tasks_for_user(session_user.id)


def _my_tasks_for_user(user_id: int) -> list[dict[str, Any]]:
    """Shared implementation behind /my-tasks (cookie auth) and
    /embed/my-tasks (signed-link auth). Both surfaces want the
    same list shape; they only differ in how the user_id is
    resolved.
    """
    rows = _assignments.list_active_for_user(user_id)
    if not rows:
        return []

    # Lookup submission + form metadata so each row is renderable
    # without extra round-trips. The set of distinct submission
    # handles is bounded by the user's active assignments — typically
    # a handful, even for power users.
    from sqlalchemy import select as _select
    from sqlalchemy.orm import Session as _DBSession

    handles = sorted({r["submission_handle"] for r in rows})
    with _DBSession(_store_module._engine) as db:
        subs = {
            s.handle: s
            for s in db.execute(
                _select(_store_module.Submission).where(
                    _store_module.Submission.handle.in_(handles)
                )
            ).scalars().all()
        }
        version_ids = {
            s.form_version_id for s in subs.values()
        }
        versions = {
            v.id: v
            for v in db.execute(
                _select(_store_module.FormVersion).where(
                    _store_module.FormVersion.id.in_(version_ids)
                )
            ).scalars().all()
        }

    out: list[dict[str, Any]] = []
    for r in rows:
        sub = subs.get(r["submission_handle"])
        if sub is None:
            continue
        version = versions.get(sub.form_version_id)
        form_id = version.form_id if version is not None else None
        form_title = (
            (version.compiled_graph or {}).get("title", form_id)
            if version is not None
            else form_id
        )
        out.append({
            "assignment_id": r["id"],
            "submission_handle": sub.handle,
            "submission_id": sub.submission_id,
            "submission_state": sub.state,
            "form_id": form_id,
            "form_title": form_title,
            "role_id": r["role_id"],
            "granted_at": r["granted_at"],
        })
    return out


# --- Picker options endpoint (Phase 3 follow-up) ---------------------------
#
# Pickers (@users / @users.external / @users.email / @users.groups)
# resolve their option list server-side via the developer-provided
# resolver. The frontend fetches the resolved options from this
# endpoint when the dropdown opens.


@api.get("/forms/{form_id}/pickers/{node_id}/{input_id}/options")
def picker_options(
    form_id: str,
    node_id: str,
    input_id: str,
    frontflow_session: str | None = Cookie(default=None),
    key: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Return the resolved option list for a picker input.

    Two-shape response:
      {
        "options": [
          {"value": <identifier>, "label": <display string>},
          ...
        ],
        "identifier_kind": "frontflow_user_id" | "external_id" | ...
      }

    Values are the identifiers the picker produces (what
    Assign(to=...) consumes). Labels are resolved server-side
    based on identifier_kind:
      - frontflow_user_id → username
      - frontflow_group_id → group name
      - external_id → username if the row exists; else the
        external_id itself
      - email → the email address (no resolution needed)

    Gated by form visibility — the same check that gates the
    form-render endpoints. A signed-link token visitor passes too.
    """
    from frontflow.dsl.core import WORKFLOWS
    from frontflow.dsl.pickers import PickerInput

    # Form visibility gate. Pickers depend on the form being
    # reachable; restricted forms 404 anonymous visitors. Pass an
    # empty submission_handle since picker options aren't bound
    # to a specific submission — the visibility check uses form
    # rules only when no token is present.
    user = auth.resolve_session(frontflow_session)
    if not auth.can_access_form(user, form_id, key):
        # Token bypass: a signed-link token implicitly grants
        # form-level access for that submission. We don't have a
        # submission handle here, so accept any token whose
        # signature + expiry verify; the picker output isn't
        # sensitive in the way submission data is.
        if token:
            from frontflow.dsl import signed_links as _signed_links
            # Try wildcard (embed) first, then any handle.
            ok = _signed_links.verify(
                token, submission_handle="*",
            ) is not None
            if not ok:
                # Defer to the form-visibility caller's
                # responsibility: still 404 since we can't bind
                # the token to a form-level grant cleanly.
                raise HTTPException(
                    status_code=404, detail="not found",
                )
        else:
            raise HTTPException(
                status_code=404, detail="not found",
            )

    # Resolve the picker via the live WORKFLOWS registry — we
    # need the Python resolver callable, which doesn't survive
    # compilation.
    wf = WORKFLOWS.get(form_id)
    if wf is None:
        raise HTTPException(
            status_code=404, detail=f"form {form_id!r} not found",
        )
    # Find the node + picker input.
    picker = _find_picker_in_workflow(wf, node_id, input_id)
    if picker is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no picker input named {input_id!r} on node "
                f"{node_id!r} of form {form_id!r}"
            ),
        )

    # Build a minimal context for the resolver. Phase 4+ fills
    # this with more — for now, the resolver gets the user (or
    # None for anonymous), the form_id, and the node_id.
    ctx = {
        "user": user,
        "form_id": form_id,
        "node_id": node_id,
    }
    try:
        identifiers = picker.resolver(ctx) or []
    except Exception as e:  # noqa: BLE001 — return 500 with detail
        raise HTTPException(
            status_code=500,
            detail=f"picker resolver raised: {e}",
        )

    # Resolve identifiers to display labels.
    labels = _resolve_picker_labels(
        identifiers, kind=picker.identifier_kind,
    )
    options = [
        {"value": ident, "label": labels.get(ident, str(ident))}
        for ident in identifiers
    ]
    return {
        "options": options,
        "identifier_kind": picker.identifier_kind,
    }


def _find_picker_in_workflow(
    wf: Any, node_id: str, input_id: str,
) -> Any:
    """Walk a Workflow's nodes (top-level + page sections) to find
    a PickerInput by (node_id, input_id). Returns None if not
    found or if the input isn't a picker."""
    from frontflow.dsl.core import Node as _Node, Page as _Page, Operator as _Op
    from frontflow.dsl.pickers import PickerInput as _PickerInput

    def _scan_node(n: Any) -> Any:
        if n.id != node_id:
            return None
        return _walk_for_picker(n.layout, input_id)

    for step in wf.steps:
        if isinstance(step, _Node):
            found = _scan_node(step)
            if found is not None:
                return found
        elif isinstance(step, _Page):
            for sec in step.nodes:
                found = _scan_node(sec)
                if found is not None:
                    return found
    return None


def _walk_for_picker(op: Any, input_id: str) -> Any:
    """Walk a layout tree (Operator + container children) looking
    for a PickerInput whose id matches."""
    from frontflow.dsl.core import Operator as _Op
    from frontflow.dsl.pickers import PickerInput as _PickerInput
    if op is None:
        return None
    if isinstance(op, _PickerInput) and op.id == input_id:
        return op
    if isinstance(op, _Op):
        for child in getattr(op, "downstream", ()) or ():
            r = _walk_for_picker(child, input_id)
            if r is not None:
                return r
    for attr in ("children", "items"):
        for child in getattr(op, attr, ()) or ():
            r = _walk_for_picker(child, input_id)
            if r is not None:
                return r
    return None


def _resolve_picker_labels(
    identifiers: list[Any], *, kind: str,
) -> dict[Any, str]:
    """Bulk-resolve a list of picker identifiers to their display
    labels. One DB query per kind (or none, for `email` where the
    identifier IS the label).

    `kind` must be one of the picker `identifier_kind` values:
      - `frontflow_user_id`   → User.username
      - `frontflow_group_id`  → Group.name
      - `external_id`         → User.username keyed by User.external_id
      - `email`               → identifier-as-label (no lookup)

    Keys in the returned dict are the SAME Python type as the
    inputs (no str-coerce here — that's the caller's job when JSON
    serialization needs string keys). Identifiers that don't match
    a row are absent from the result; the caller falls back to
    str(identifier) for display.

    Originally inlined inside picker_options. Lifted out when the
    submission-detail page also needed it (to render picker
    values as labels instead of raw ids). Both call sites resolve
    on the request thread — fine because the queries are tiny and
    bounded by the number of options / step values.
    """
    if not identifiers:
        return {}
    from sqlalchemy import select as _select
    from sqlalchemy.orm import Session as _DBSession
    from frontflow.dsl.store import (
        User as _User, _engine,
    )
    # The Group model is optional — only present if the schema has
    # been migrated. Defensive import so an old DB without the
    # table still works for user/email pickers.
    try:
        from frontflow.dsl.store import Group as _Group  # type: ignore
    except ImportError:
        _Group = None

    if kind == "email":
        # Email IS the label. No DB hop.
        return {ident: str(ident) for ident in identifiers}

    if kind == "frontflow_user_id":
        ids = [int(i) for i in identifiers if isinstance(i, (int, str))]
        if not ids:
            return {}
        with _DBSession(_engine) as db:
            rows = db.execute(
                _select(_User.id, _User.username).where(
                    _User.id.in_(ids),
                )
            ).all()
        return {row[0]: row[1] for row in rows}

    if kind == "external_id":
        ids = [str(i) for i in identifiers if isinstance(i, (int, str))]
        if not ids:
            return {}
        with _DBSession(_engine) as db:
            rows = db.execute(
                _select(_User.external_id, _User.username).where(
                    _User.external_id.in_(ids),
                )
            ).all()
        return {row[0]: row[1] for row in rows}

    if kind == "frontflow_group_id":
        if _Group is None:
            return {}
        ids = [int(i) for i in identifiers if isinstance(i, (int, str))]
        if not ids:
            return {}
        with _DBSession(_engine) as db:
            rows = db.execute(
                _select(_Group.id, _Group.name).where(
                    _Group.id.in_(ids),
                )
            ).all()
        return {row[0]: row[1] for row in rows}

    # Unknown kind — return empty so the caller falls back to str().
    return {}


def _resolve_step_picker_labels(
    node: Any, form_values: dict[str, Any],
) -> tuple[
    Optional[dict[str, dict[str, str]]],
    Optional[dict[str, str]],
]:
    """Build the `{field_name: {identifier: label}}` map for every
    picker field in `node` that has a value in `form_values`, plus a
    `{field_name: identifier_kind}` map so the frontend can decide
    which fields to wrap in a link to the user record.

    Used by the submission detail builder to surface human-readable
    labels (e.g. usernames) alongside the raw identifiers (e.g.
    user_ids) — so `recruiter_picker: 1` renders as `recruiter_picker: admin`
    instead of leaving the user to guess what user_id=1 means.

    Returns (None, None) when the node has no picker fields with
    values. Bulk-resolves per identifier_kind so each kind costs
    one DB query at most.
    """
    out: dict[str, dict[str, str]] = {}
    kinds: dict[str, str] = {}
    # Bucket identifiers by kind so we issue at most one DB query
    # per kind. (A node typically has one picker; bucketing is
    # essentially free.)
    by_kind: dict[str, dict[str, list[Any]]] = {}
    for f in node.fields:
        if getattr(f, "type", None) != "picker":
            continue
        kind = getattr(f, "identifier_kind", None)
        if kind is None:
            continue
        v = form_values.get(f.name)
        if v is None:
            continue
        ids = v if isinstance(v, list) else [v]
        if not ids:
            continue
        by_kind.setdefault(kind, {})[f.name] = ids
        kinds[f.name] = kind

    for kind, fields in by_kind.items():
        flat = [i for ids in fields.values() for i in ids]
        labels = _resolve_picker_labels(flat, kind=kind)
        for field_name, ids in fields.items():
            entry: dict[str, str] = {}
            for ident in ids:
                # JSON keys must be strings — coerce here so the
                # frontend can index uniformly regardless of whether
                # the underlying type is int (user_id) or str
                # (external_id / email).
                key = str(ident)
                label = labels.get(ident)
                if label is None:
                    # Fallback: stale id no longer present in users
                    # / groups table; render as the id itself.
                    label = str(ident)
                entry[key] = label
            if entry:
                out[field_name] = entry
    return (out or None, kinds or None)



    """Bulk-resolve a list of identifiers to display labels per
    identifier_kind. Returns a {identifier: label} dict; missing
    entries fall back to str(identifier) at the caller."""
    if not identifiers:
        return {}

    from sqlalchemy import select as _select
    from sqlalchemy.orm import Session as _DBSession
    from frontflow.dsl.store import (
        Group as _Group, User as _User, _engine,
    )

    if kind == "email":
        # Email labels ARE the email; no DB roundtrip.
        return {ident: str(ident) for ident in identifiers}

    if kind == "frontflow_user_id":
        ids = [int(i) for i in identifiers if isinstance(i, (int, str))]
        with _DBSession(_engine) as db:
            rows = db.execute(
                _select(_User.id, _User.username).where(
                    _User.id.in_(ids),
                )
            ).all()
        return {row[0]: row[1] for row in rows}

    if kind == "external_id":
        ext_ids = [str(i) for i in identifiers]
        with _DBSession(_engine) as db:
            rows = db.execute(
                _select(_User.external_id, _User.username).where(
                    _User.external_id.in_(ext_ids),
                )
            ).all()
        out = {row[0]: row[1] for row in rows}
        # Identifiers without a local mirror render as the
        # external_id — better than blank.
        for ext_id in ext_ids:
            out.setdefault(ext_id, ext_id)
        return out

    if kind == "frontflow_group_id":
        ids = [int(i) for i in identifiers if isinstance(i, (int, str))]
        with _DBSession(_engine) as db:
            rows = db.execute(
                _select(_Group.id, _Group.name).where(
                    _Group.id.in_(ids),
                )
            ).all()
        return {row[0]: row[1] for row in rows}

    return {}


# --- Embed /my-tasks (Phase 6) --------------------------------------------
#
# Install-wide allowlist + signed-token-based authentication for an
# external system to embed a user's task inbox in an iframe. Distinct
# from the per-form iframe surface (which is for embedding a form);
# this is for embedding the user's whole inbox so they can see what's
# pending without leaving the host page.
#
# Authentication: the host page is responsible for minting an
# embed-scope signed token for the right user (via
# signed_links.mint_for_embed) and including it as `?token=...` on
# the iframe src. The token's `issuer` must be "embed" — assign-
# operator tokens are rejected to prevent confused-deputy use.
#
# CSP: the security_headers middleware honors the install-wide
# FRONTFLOW_EMBED_ALLOWED_ORIGINS env var on /api/embed/* paths.
# Without it the route returns 404 (existence not leaked).


@api.get("/embed/my-tasks")
def embed_my_tasks(
    token: str | None = None,
) -> list[dict[str, Any]]:
    """Embed-mode /my-tasks. Requires an embed-scope signed token
    AND the install must have FRONTFLOW_EMBED_ALLOWED_ORIGINS set
    — without that, the route 404s.

    Returns the same shape as /api/my-tasks. The bearer of a
    valid embed token sees only their own active assignments.
    """
    # Refuse the route entirely when the install hasn't opted in
    # to embed. Matches how restricted forms behave — existence
    # is not leaked.
    if not _embed_allowed_origins():
        raise HTTPException(
            status_code=404, detail="not found"
        )
    if not token:
        raise HTTPException(
            status_code=401, detail="embed token required"
        )
    from frontflow.dsl import signed_links as _signed_links
    payload = _signed_links.verify(
        token, submission_handle="*", require_issuer="embed",
    )
    if payload is None:
        raise HTTPException(
            status_code=401, detail="invalid or expired embed token"
        )
    # Verify the user exists + is active.
    from frontflow.dsl.store import User as _User
    from sqlalchemy.orm import Session as _DBSession
    with _DBSession(_store_module._engine) as db:
        user = db.get(_User, payload["user_id"])
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=401, detail="user not active"
        )
    return _my_tasks_for_user(payload["user_id"])


# --- Serve the bundled frontend --------------------------------------------
#
# Every data route is mounted under /api here — registered before the
# SPA catch-all below so API calls always win. The built React app
# ships inside the package at frontflow/static/; any non-/api, non-asset
# path returns index.html so the single-page app handles the route.

class WorkspaceSummary(BaseModel):
    workspace_id: str
    title: str
    description: str = ""
    tags: list[str] = []


@api.get("/workspaces", response_model=list[WorkspaceSummary])
def list_workspaces(
    frontflow_session: str | None = Cookie(default=None),
) -> list[WorkspaceSummary]:
    """Workspaces the caller may open.

    Filtered rather than gated: a listing that 403s would tell an
    unauthorized visitor which workspaces exist.
    """
    user = auth.resolve_session(frontflow_session)
    return [
        WorkspaceSummary(
            workspace_id=ws_id,
            title=layout["title"],
            description=layout.get("description", ""),
            tags=layout.get("tags", []),
        )
        for ws_id, layout in sorted(WORKSPACE_LAYOUTS.items())
        if auth.can_access_workspace(user, ws_id)
    ]


@api.get(
    "/workspaces/{workspace_id}",
    dependencies=[Depends(require_workspace_visibility)],
)
def read_workspace(
    workspace_id: str,
    frontflow_session: str | None = Cookie(default=None),
    key: str | None = None,
) -> dict[str, Any]:
    """A workspace's panel tree — the arrangement the browser docks.

    Carries the caller's role so the UI knows whether to offer the
    Superset edit surface on dashboard panels. Forms are not editable
    from the UI at all; their definition lives in the DSL.
    """
    layout = WORKSPACE_LAYOUTS.get(workspace_id)
    if layout is None:
        raise HTTPException(
            status_code=404, detail=f"workspace {workspace_id!r} not found"
        )

    user = auth.resolve_session(frontflow_session)
    role = auth.workspace_access(user, workspace_id, key)
    return {
        **layout,
        "access": role or "view",
        # Whether to offer the Superset dashboard editor. Superset still
        # decides what the person can actually save, via their own login.
        "can_edit_dashboards": role == "manage",
    }


# Superset dashboard routes live in their own module — this file is
# large enough already. Registered here rather than imported at module
# top so a Superset failure cannot take down the whole app, and so an
# install without the optional extra is unaffected.
try:
    from .superset import routes as _superset_routes

    _superset_routes.register(
        api,
        require_form_visibility=require_form_visibility,
        require_workspace_visibility=require_workspace_visibility,
        require_admin=require_admin,
    )
except Exception as exc:  # noqa: BLE001 - optional integration
    # main.py imports logging locally rather than at module scope
    # (see the same pattern above); match that convention.
    import logging

    logging.getLogger("frontflow").info(
        "Superset routes not registered (%s)", exc
    )


app.include_router(api, prefix="/api")

from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

_STATIC_DIR = Path(__file__).parent / "static"


class _ImmutableAssetsStaticFiles(StaticFiles):
    """A `StaticFiles` that brands every response with a long-lived
    immutable `Cache-Control`.

    The asset filenames Vite emits are content-hashed (e.g.
    `query-vendor-BELYcS-0.js`) — different content gets a different
    name, so the same name is always the same bytes. Telling the
    browser to cache them forever is safe; without this, browsers
    heuristically cache and we eat a round-trip per asset on every
    SPA load.

    Index.html is NOT served from here (the SPA catch-all handles
    it) and gets its own `no-cache` header — see `_spa` below for
    the why."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable"
        )
        return resp


if _STATIC_DIR.is_dir():
    # Hashed build assets live under /assets — mount them directly.
    _assets = _STATIC_DIR / "assets"
    if _assets.is_dir():
        app.mount(
            "/assets",
            _ImmutableAssetsStaticFiles(directory=_assets),
            name="assets",
        )

    @app.get("/{full_path:path}", include_in_schema=False)
    def _spa(full_path: str) -> FileResponse:
        """Serve a bundled static file, or index.html for any other
        path so the single-page app can handle the route itself.

        `index.html` is served with `Cache-Control: no-cache` —
        the file's *contents* change on every deploy (it embeds the
        current build's content-hashed asset filenames in <script>
        tags), but its URL never changes. Without `no-cache`,
        browsers heuristically cache the HTML and continue
        referencing the previous build's asset hashes after a
        deploy — those filenames no longer exist on disk and
        404 the SPA into a broken state. `no-cache` doesn't disable
        caching; it forces a revalidation request each time, which
        the browser handles via 304 when the file is unchanged."""
        # An unmatched /api path is a genuine 404 — never the SPA.
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="not found")
        candidate = (_STATIC_DIR / full_path).resolve()
        # Stay inside the static dir; serve the file if it exists.
        if (
            _STATIC_DIR in candidate.parents
            and candidate.is_file()
        ):
            # Non-/assets static files (favicon, manifest, etc.)
            # — short cache, not immutable; their filenames aren't
            # content-hashed.
            return FileResponse(
                candidate,
                headers={"Cache-Control": "public, max-age=3600"},
            )
        return FileResponse(
            _STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-cache"},
        )
