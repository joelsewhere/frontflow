"""
Persistence for the form-builder data backend.

The runtime keeps submissions in memory as its working set; this module
is the durable mirror. The model:

  form          — a form's stable identity (the DSL workflow id).
  form_version  — a snapshot of one compiled state of a form: the
                  render-ready `compiled_graph` JSON plus the DSL
                  `source` it was compiled from, so any version can be
                  both viewed and re-executed.
  submission    — one user's traversal, pinned to the form_version it
                  ran against.
  step          — the submission's current execution chain (a reset
                  truncates it).
  event         — an append-only log of lifecycle events; this is what
                  survives a reset and powers the analytics.

Integration is write-through: the runtime calls `sync_submission` at the
end of each public operation. A submission is mirrored only once its id
is minted — before that it's an in-memory session draft (see runtime).
Boot calls `load_submissions` to rehydrate the working set.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    func,
    inspect,
    or_,
    select,
    update,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    selectinload,
)

from .crypto import decrypt_secret, encrypt_secret

# --- Engine ----------------------------------------------------------------
#
# The database is configurable. By default frontflow uses a SQLite file
# in a per-user data directory (~/.frontflow/forms.db) — zero setup, fine
# for a single process. For a shared or production deployment, point it
# at Postgres with the DATABASE_URL environment variable:
#
#   DATABASE_URL=postgresql+psycopg://user:pass@host:5432/frontflow
#
# DB_PATH still works as a shorthand for a specific SQLite file, and
# FRONTFLOW_HOME relocates the default data directory.


def _default_data_dir() -> Path:
    home = os.environ.get("FRONTFLOW_HOME")
    base = Path(home) if home else Path.home() / ".frontflow"
    return base


def _resolve_database_url() -> str:
    """Determine the SQLAlchemy database URL.

    Precedence: an explicit DATABASE_URL wins; otherwise a SQLite file
    at DB_PATH (or the default data directory). When SQLite is used,
    its parent directory is created.

    Several platforms (Heroku, Render, Railway) provision Postgres
    and hand back a connection string starting with `postgres://` —
    the legacy prefix SQLAlchemy 2.x rejects. We rewrite it to the
    modern `postgresql+psycopg://` form so the same env var works on
    every platform without the user knowing about the footgun.
    """
    explicit = os.environ.get("DATABASE_URL")
    if explicit:
        # Normalize legacy Postgres scheme. `postgres://` and the
        # ambiguous `postgresql://` (no driver) both get pinned to
        # `postgresql+psycopg://` so SQLAlchemy resolves the driver
        # the same way locally as it does on platforms that auto-set
        # `DATABASE_URL`. Users who want a different driver (asyncpg,
        # pg8000) can still set the explicit form themselves; we
        # only rewrite the ambiguous prefixes.
        if explicit.startswith("postgres://"):
            explicit = "postgresql+psycopg://" + explicit[len("postgres://"):]
        elif explicit.startswith("postgresql://"):
            explicit = "postgresql+psycopg://" + explicit[len("postgresql://"):]
        return explicit
    db_path = Path(
        os.environ.get("DB_PATH", _default_data_dir() / "forms.db")
    ).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_path}"


DATABASE_URL = _resolve_database_url()
_is_sqlite = DATABASE_URL.startswith("sqlite")

# SQLite needs check_same_thread disabled (FastAPI serves on a thread
# pool); other drivers neither need nor accept that argument.
_connect_args = {"check_same_thread": False} if _is_sqlite else {}

_engine = create_engine(
    DATABASE_URL,
    future=True,
    connect_args=_connect_args,
    # Recycle connections so a Postgres deployment survives the server
    # closing idle connections; harmless for SQLite.
    pool_pre_ping=not _is_sqlite,
)


# --- Models ----------------------------------------------------------------


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Re-attach UTC to a datetime read back from SQLite. SQLite has no
    tz-aware type, so stored datetimes round-trip as naive; everything
    written is UTC, so naive values are reinterpreted as UTC. Keeps the
    runtime's "all datetimes are UTC-aware" invariant intact."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class Form(Base):
    """A form's stable identity. Upserted from the DSL on every scan."""

    __tablename__ = "form"

    form_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # Relative directory of the form's file under the workflows dir —
    # drives the folder-aware admin listing.
    folder_path: Mapped[str] = mapped_column(String, default="")
    # Whether the DSL file is still present in the most recent scan.
    is_live: Mapped[bool] = mapped_column(Boolean, default=True)
    # Per-form theme — the token set the form-facing views render with.
    # Null until customized; the frontend falls back to its default.
    theme: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # Who may reach the form-filling surface:
    #   public     — anyone, no sign-in
    #   unlisted   — anyone with the link's unlisted_token
    #   restricted — only permitted signed-in users (FormACL)
    # A folder grant always confers access regardless of this — the
    # check is additive (see auth.can_access_form).
    visibility: Mapped[str] = mapped_column(
        String, nullable=False, default="public"
    )
    # The unguessable key for unlisted mode. Set when a form goes
    # unlisted; regenerable (which invalidates old links).
    unlisted_token: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    versions: Mapped[list["FormVersion"]] = relationship(
        back_populates="form",
        order_by="(FormVersion.version, FormVersion.minor_version)",
    )


class FormVersion(Base):
    """A snapshot of one compiled state of a form. A new row is written
    on every real change to either the compiled structure (a `version`
    bump — structural / "major") or the raw source text alone (a
    `minor_version` bump — non-structural / "minor", e.g. edits to
    helper functions that the compiled graph doesn't capture).

    Identity is the (form_id, version, minor_version) tuple. The
    legacy `(form_id, content_hash)` unique constraint can't be kept
    because minor-bumped rows intentionally share a content_hash with
    their structural ancestor."""

    __tablename__ = "form_version"
    __table_args__ = (
        UniqueConstraint("form_id", "version", "minor_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    form_id: Mapped[str] = mapped_column(
        ForeignKey("form.form_id"), nullable=False, index=True
    )
    # Monotonic per form — the human-facing "version 3". Bumps only
    # on structural change (different compiled-graph content hash).
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Monotonic within (form_id, version) — bumps when the source text
    # changes but the compiled graph is identical (helper functions,
    # comments, formatting). Resets to 0 on every major bump. Rendered
    # as the `.N` suffix in `v{version}.{minor_version}`; suppressed
    # when 0 so existing forms still display as `v3` not `v3.0`.
    minor_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    # Render-ready structure snapshot (serialize_workflow output).
    compiled_graph: Mapped[dict] = mapped_column(JSON, nullable=False)
    # The DSL source the form was compiled from — recompiled to an
    # executable graph when an old-version submission must advance.
    source: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    form: Mapped["Form"] = relationship(back_populates="versions")


class Submission(Base):
    """One user's traversal, pinned to the form_version it ran on."""

    __tablename__ = "submission"

    # The stable internal key, assigned at creation (before an id exists).
    handle: Mapped[str] = mapped_column(String, primary_key=True)
    # The minted public id. Always set once a row exists (persistence
    # begins at mint), but kept nullable to mirror the runtime type.
    submission_id: Mapped[Optional[str]] = mapped_column(
        String, unique=True, nullable=True, index=True
    )
    form_version_id: Mapped[int] = mapped_column(
        ForeignKey("form_version.id"), nullable=False, index=True
    )
    # running | success | failed
    state: Mapped[str] = mapped_column(String, nullable=False)
    # 'submission' | 'session'.
    #
    # A control panel — a form whose node declares `closes=False` — is a
    # working surface, not a submission. It never routes, so it never
    # terminates, and left as an ordinary row it sits at `running`
    # forever: it inflates every count and, worse, flows into
    # `v_frontflow_submissions`, where a panel's own field values become
    # analytics data. That is not hypothetical — a filter widget briefly
    # named `units` put objects into a column another form stores
    # numbers in, and broke every chart reading it.
    #
    # Persisted rather than derived: the counts and the reporting view
    # are SQL over this table and cannot see the DSL.
    kind: Mapped[str] = mapped_column(
        String, nullable=False, default="submission", index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Bumped on every state change — a new step, a clear, termination.
    # Drives the export API's `updated_since` incremental re-sync.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    terminated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    # Soft-delete tombstone. NULL = active; non-NULL = the moment an
    # admin clicked "delete" in the submissions listing. We never
    # hard-delete rows — the Step / Event / SubmissionBlob audit
    # trail stays intact, and an undelete endpoint (out of scope
    # for this round) can lift the tombstone if needed. Every
    # user-facing read path filters `WHERE deleted_at IS NULL` so
    # tombstoned rows simply disappear from the UI. Indexed because
    # the filter is on the hot path (listing, hydration, counts).
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, index=True,
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Pre-clear Airflow run ids stashed by an edit, so a replayed
    # `trigger_dag` with an unchanged explicit run id can re-attach to
    # the cleared run rather than POST a new one. Keyed by operator;
    # see Submission.cleared_run_ids in the runtime. JSON — small and
    # bounded; empty for submissions with no cleared Airflow triggers.
    cleared_run_ids: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True
    )
    # Parent-child linkage — set when this submission was spawned by
    # an `Assign` operator on another submission. Nullable: a
    # user-initiated submission has no parent. The columns are kept
    # on the row (rather than in a join table) because v1 is single-
    # parent — a multi-parent model is out of scope and would need
    # a separate table.
    parent_submission_handle: Mapped[Optional[str]] = mapped_column(
        ForeignKey("submission.handle"), nullable=True, index=True,
    )
    # Which Assign call on the parent spawned us. The node_id +
    # operator-index pair identifies the specific Assign so the
    # parent's submission-detail UI can group its children by
    # the call that produced them. Nullable when there's no parent.
    parent_assign_node_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    parent_assign_op_idx: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )

    steps: Mapped[list["Step"]] = relationship(
        back_populates="submission",
        order_by="(Step.form_version_id, Step.seq)",
        cascade="all, delete-orphan",
    )
    events: Mapped[list["Event"]] = relationship(
        back_populates="submission",
        order_by="Event.id",
        cascade="all, delete-orphan",
    )


class Step(Base):
    """A step in the submission's *current* execution chain. A reset
    deletes the truncated rows — the event log retains the history."""

    __tablename__ = "step"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    submission_handle: Mapped[str] = mapped_column(
        ForeignKey("submission.handle"), nullable=False, index=True
    )
    # The form_version this step was created under. Stays fixed for
    # the row's lifetime — a force re-pin freezes existing steps by
    # leaving this unchanged and starting a fresh active chain at the
    # new version. Reads default to the submission's current version
    # (the active chain); history reads include prior versions.
    form_version_id: Mapped[int] = mapped_column(
        ForeignKey("form_version.id"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    node_id: Mapped[str] = mapped_column(String, nullable=False)
    page_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # node | backend
    kind: Mapped[str] = mapped_column(String, nullable=False)
    # awaiting | submitted | failed
    state: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    submitted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    form_values: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # A step's @backend return value, when it has one — part of the
    # step's data and needed to resume a submission after a restart.
    backend_return: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    # Per-chain-step output, keyed by the producer's name (an `@backend`
    # function's name or an Airflow operator's task id). Each value is
    # `{state, return, detail}`. Used by the runtime to resume chains
    # across restarts (an in-flight Airflow run id must survive a
    # reload), and surfaced to the submission-detail UI so users can
    # see every backend's output (not just the legacy first one).
    external_state: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True
    )
    button_clicked: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    next_node_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    branch_explicit: Mapped[bool] = mapped_column(Boolean, default=False)
    # Per-step error message. Set when a backend or chain step raised;
    # `state` is then "failed". Persisting per-step (instead of only at
    # the submission level) lets the chain UI surface WHICH step's
    # output blew up and what the exception was, without the user
    # needing to dig through server logs.
    error: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Full Python traceback for the same failure — captured at the
    # except site so the chain UI can render the stack frames in a
    # collapsible details panel. Null on non-failed steps and on
    # legacy rows from before this column existed.
    traceback: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # The user who SUBMITTED this step. Null until submitted, and
    # null for legacy step rows from before this column existed. A
    # single submission can accumulate many distinct user_ids across
    # its steps when different actors handle different nodes (e.g.
    # an assignee fills a node assigned to them via `Assign`, then
    # a downstream node is filled by a different assignee). The
    # set of distinct values is the submission's "contributors";
    # the visibility gate reads this column to decide who may view
    # the submission.
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("app_user.id"), nullable=True, index=True,
    )

    submission: Mapped["Submission"] = relationship(back_populates="steps")


class Event(Base):
    """An append-only lifecycle event. Reconstructs history that the
    current-state tables drop — resets, dropoff, per-attempt timing."""

    __tablename__ = "event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    submission_handle: Mapped[str] = mapped_column(
        ForeignKey("submission.handle"), nullable=False, index=True
    )
    # submission_created | id_minted | step_started | step_submitted |
    # step_reset | submission_terminated | submission_failed
    type: Mapped[str] = mapped_column(String, nullable=False)
    node_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    page_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # The form version this event was recorded under. Nullable for
    # rows that existed before the column was introduced; populated on
    # every new event by the runtime. Lets the history viewer scope
    # events to the version currently being viewed.
    form_version_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("form_version.id"), nullable=True, index=True
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    submission: Mapped["Submission"] = relationship(back_populates="events")


class Comment(Base):
    """A comment on a component of a submission. Threads are keyed by
    (form_id, submission_handle, thread_id) — `thread_id` names the
    component (a `displays.Comments` block id today; finer-grained
    anchors later). Append-only for now."""

    __tablename__ = "comment"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    form_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    submission_handle: Mapped[str] = mapped_column(
        String, nullable=False, index=True
    )
    thread_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # Display name; user_id when an authenticated session made it.
    author: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Connection(Base):
    """A stored credentialed endpoint — currently Airflow instances.

    Workflow operators reference a connection by `name`. The credential
    payload is Fernet-encrypted (see workflows/crypto.py) before it
    touches the database, so the SQLite file never holds plaintext
    usernames, passwords, or tokens. `base_url`, `conn_type`, and
    `auth_kind` are not sensitive and stay in the clear."""

    __tablename__ = "connection"

    # The name is the stable identifier operators wire against.
    name: Mapped[str] = mapped_column(String, primary_key=True)
    # Endpoint family — 'airflow' for now.
    conn_type: Mapped[str] = mapped_column(String, nullable=False)
    base_url: Mapped[str] = mapped_column(String, nullable=False)
    # How the secret is shaped — 'basic' (username/password) or 'token'.
    auth_kind: Mapped[str] = mapped_column(String, nullable=False)
    # Fernet token over the JSON credential payload.
    secret: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Workspace(Base):
    """A workspace's stable identity and visibility.

    Upserted from the DSL on every scan, mirroring `Form`. The DSL's
    `private=` sets the INITIAL visibility on first discovery; after
    that an admin owns it through the API, so a re-scan does not undo a
    deliberate change.

    Visibility matters more here than on a form: a workspace is what
    authorizes its dashboard panels. Outside a form there is no form ACL
    to inherit, so this row is the gate.
    """

    __tablename__ = "workspace"

    workspace_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # public | unlisted | restricted — same three-way model as forms.
    visibility: Mapped[str] = mapped_column(
        String, nullable=False, default="public"
    )
    unlisted_token: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    # Whether the DSL file is still present in the most recent scan.
    is_live: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )


class WorkspaceAcl(Base):
    """Per-user access to a workspace. Mirrors app_form_acl, plus a role.

    'view' opens the workspace; 'manage' additionally allows editing the
    dashboards inside it. Admins always have 'manage'.

    The role gates whether frontflow *offers* the edit surface. It does
    not confer Superset rights — frontflow users are not Superset users,
    so what a person can actually save is still decided by their own
    Superset login.
    """

    __tablename__ = "app_workspace_acl"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspace.workspace_id"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id"), primary_key=True
    )
    # 'view' | 'manage'
    role: Mapped[str] = mapped_column(String, nullable=False, default="view")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class DashboardBinding(Base):
    """A Superset dashboard, addressed by the logical name a workflow uses.

    Workflow authors write `displays.Dashboard("sales_overview")` — a
    name, never an id. This table is what turns that name into the two
    opaque values the embed actually needs:

      - `embed_uuid`: from Superset's "Embed dashboard" config. NOT the
        numeric dashboard id — using that instead yields a silently
        blank iframe with no error, which is a genuinely hard failure to
        diagnose, hence both are stored separately.
      - `filter_id`: the native time-range filter that
        `superset.RefreshDashboard` drives to force an in-place
        re-query. Without it a dashboard still renders, but never
        updates in response to the chain.

    A name with no row here is provisioned on first use (see
    `frontflow.superset.provisioning`), which is what makes a dashboard
    referenced in a workflow "just work" against an empty Superset.

    `auto_created` records whether frontflow made the dashboard itself.
    Only auto-created dashboards are safe to delete on cleanup — a
    dashboard someone hand-built and then adopted must outlive its
    binding.
    """

    __tablename__ = "dashboard_binding"

    # The logical name workflows reference.
    name: Mapped[str] = mapped_column(String, primary_key=True)
    # Which Superset instance this lives on — the `connection` row's
    # name. Stored so an install can address more than one Superset,
    # the same way operators can target more than one Airflow.
    connection_name: Mapped[str] = mapped_column(String, nullable=False)
    # Superset's own numeric dashboard id, as a string.
    superset_dashboard_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    embed_uuid: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    filter_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    auto_created: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )


class Variable(Base):
    """An install-scoped configuration value, referenced by name.

    Variables hold non-secret install config — a bucket name, a default
    region, a webhook URL — that workflow authors reference via
    `{{ variables.<name> }}` in operator templates, or
    `variables.get("name")` at workflow load. Distinct from
    `Connection`: connections hold credentials and are bound to a
    transport (Airflow, AWS); variables are bare strings.

    The value is Fernet-encrypted at rest as a defense-in-depth
    measure. The intended use is non-secret, but users will put
    secrets here regardless — encrypting protects those who
    misuse. Connections remain the right home for credentials
    because they thread auth through to operators."""

    __tablename__ = "variable"

    # The name is the stable identifier templates wire against. Same
    # slug shape as connections, but a separate namespace — a
    # connection and a variable can share a name without collision.
    name: Mapped[str] = mapped_column(String, primary_key=True)
    # Optional human-readable note shown in the admin UI. Helps the
    # next operator remember what a value is for.
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Fernet token over a JSON object {"value": "<the string>"}.
    # Wrapping in a dict reuses the existing encrypt_secret helper
    # without changing its signature.
    value_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class User(Base):
    """An operator account for the admin console.

    M1 of authentication: hosted accounts with a password. The identity
    source is deliberately kept separable — a future SSO provider would
    create/resolve User rows without password_hash, and the
    authorization model (groups, grants) keys on the user, not on how
    they authenticated.
    """

    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    username: Mapped[str] = mapped_column(
        String, unique=True, nullable=False, index=True
    )
    # argon2id hash. Nullable so an SSO-sourced user can exist without
    # a local password.
    password_hash: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # A deactivated account cannot authenticate; the row and its
    # history are retained.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Set when an admin resets the password — forces a change at the
    # next login before the console is usable.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow
    )
    # Foreign-key reference to the user's identity in an external
    # system (LMS, HR system, SSO IdP). The Canvas-SIS-id model: the
    # external system is source of truth; frontflow keeps a mapping
    # plus whatever local state (assignments, submissions) it owns.
    # Nullable — a frontflow-only user (admin, no external mirror)
    # has external_id = None. Unique when set; two User rows cannot
    # share an external_id. Indexed for resolve-by-external_id
    # lookups, which fire on every notification handler and signed
    # link verification path.
    external_id: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, unique=True, index=True,
    )
    # Per-user notification opt-out state. Free-form JSON dict
    # mapping channel name → bool ("email": True, "slack": False).
    # frontflow does NOT enforce this — handlers read it and decide.
    # Channels are open-ended strings; no fixed enum. Default empty
    # dict (handlers should treat missing keys as "send"). The
    # in-app inbox at /my-tasks is not gated by this; it's the
    # floor (design doc P5).
    notification_preferences: Mapped[dict] = mapped_column(
        JSON, default=dict, nullable=False,
    )


class AuthSession(Base):
    """A server-side login session. DB-backed so sessions survive a
    restart, can be revoked, and work with any database backend."""

    __tablename__ = "app_session"

    # An opaque random token, carried in the session cookie.
    token: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False
    )


class Group(Base):
    """A named set of users. Folder grants are made to groups, not to
    individual users, so access is managed in bulk."""

    __tablename__ = "app_group"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    name: Mapped[str] = mapped_column(
        String, unique=True, nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow
    )


class UserGroup(Base):
    """Membership — a user belongs to a group. Composite key, so a
    pair appears at most once."""

    __tablename__ = "app_user_group"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id"), primary_key=True
    )
    group_id: Mapped[int] = mapped_column(
        ForeignKey("app_group.id"), primary_key=True
    )


class FolderGrant(Base):
    """A group's access to a folder subtree.

    `folder_path` is a form folder path ("" is the root). A grant
    cascades: it covers that folder and everything beneath it. `role`
    is the access level — 'view' (see and track forms) or 'manage'
    (also edit theme, reparse).
    """

    __tablename__ = "app_folder_grant"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    group_id: Mapped[int] = mapped_column(
        ForeignKey("app_group.id"), nullable=False, index=True
    )
    # "" is the root — a grant there covers every form.
    folder_path: Mapped[str] = mapped_column(
        String, nullable=False, default=""
    )
    # 'view' | 'manage'
    role: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow
    )


class FormACL(Base):
    """A user explicitly permitted to reach a restricted form. The
    per-form allow-list — distinct from group folder grants, which
    cover whole subtrees. Access is additive: either path grants it."""

    __tablename__ = "app_form_acl"

    form_id: Mapped[str] = mapped_column(
        ForeignKey("form.form_id"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow
    )


class SubmissionAssignment(Base):
    """A grant of role R to user U on submission S.

    Append-only. A grant + revoke pair is two states of one row:
    `granted_at` is always set; `revoked_at` is null while active
    and set to the revoke timestamp once revoked. A re-grant after
    revocation inserts a NEW row — historical access windows are
    recoverable by reading all rows for the (submission, user,
    role) triple ordered by granted_at.

    The runtime check uses the partial index on
    `(submission_handle, user_id, revoked_at IS NULL)` so the
    answer to "is user U currently assigned role R on submission
    S" is O(1) at request time.

    Lives outside the Submission row (rather than as a JSON column)
    because revocation audit needs durable timestamps and there
    can be many assignments per submission.
    """

    __tablename__ = "submission_assignment"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    submission_handle: Mapped[str] = mapped_column(
        ForeignKey("submission.handle"), nullable=False, index=True,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id"), nullable=False, index=True,
    )
    # Role identifier as declared in the form's permission template.
    # Free string; validated against the form_version snapshot at
    # grant time, not by a foreign key (roles are per-form-version,
    # not per-form, so a FK isn't a clean fit).
    role_id: Mapped[str] = mapped_column(String, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow,
    )
    # Who created this assignment. A manual admin grant carries the
    # admin's user_id; an Assign-operator grant carries the user
    # whose submission ran the operator. Required (no
    # null-for-system-grant case in v1).
    granted_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id"), nullable=False,
    )
    # When granted via an Assign operator, the parent submission's
    # handle. Null for manual admin grants — distinguishes the two
    # paths in audit.
    granted_by_submission_handle: Mapped[Optional[str]] = mapped_column(
        ForeignKey("submission.handle"), nullable=True,
    )
    # Revocation timestamp. Null = active. Set on revoke; the row
    # is NEVER deleted, so the access window survives in audit.
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True,
    )
    # Who revoked — admin, the parent submission's owner, the
    # external system, or null when revocation hasn't happened yet.
    revoked_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("app_user.id"), nullable=True,
    )


class UploadBlob(Base):
    """The bytes of a `File` upload, held until the submission ends.

    A `File` input does not persist to S3 — but its bytes still have
    to survive from the upload request to the submission's `@backend`
    run, possibly across a restart or multiple workers. They live here
    in the database, keyed by an opaque upload token, and are deleted
    when the submission terminates. `S3File` uploads never use this
    table — their bytes go straight to S3.
    """

    __tablename__ = "upload_blob"

    # Opaque token minted by the upload endpoint; the form field's
    # submitted value carries it.
    token: Mapped[str] = mapped_column(String, primary_key=True)
    # Set once the upload is tied to a submission — lets cleanup find
    # every blob for a terminated submission. Null while still a draft.
    submission_handle: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, index=True
    )
    form_id: Mapped[str] = mapped_column(String, nullable=False)
    field_id: Mapped[str] = mapped_column(String, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    content_type: Mapped[str] = mapped_column(String, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    # The raw bytes. LargeBinary maps to BLOB / bytea per dialect.
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow
    )


class SubmissionBlob(Base):
    """Bytes produced by a `@backend` that returned binary content —
    e.g. a matplotlib figure. Stored content-addressed so identical
    bytes share a row; scoped to one submission so we can clean up
    when the submission terminates.

    The chain processor hashes a backend's `bytes` return, writes a
    row here, and replaces the return value with a small handle
    (`{kind: 'blob', hash, content_type, size}`). The `displays.Figure`
    block surfaces the handle to the browser as a proxy URL
    (`/api/.../blob/{hash}`), which streams the bytes with the right
    `Content-Type`.
    """

    __tablename__ = "submission_blob"

    # The blob's content hash (sha256, hex). Combined with the
    # submission it scopes to, this is the primary key — the same
    # bytes returned by two different submissions share their content
    # but each submission tracks its own copy so cleanup is simple.
    hash: Mapped[str] = mapped_column(String, primary_key=True)
    submission_handle: Mapped[str] = mapped_column(
        ForeignKey("submission.handle"), primary_key=True, index=True
    )
    content_type: Mapped[str] = mapped_column(String, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow
    )


def put_submission_blob(
    *,
    submission_handle: str,
    body: bytes,
    content_type: str,
) -> dict[str, Any]:
    """Store `body` against `submission_handle`, content-addressed by
    sha256. Idempotent: a repeat call with identical bytes returns the
    existing row's handle. Returns the handle dict the chain processor
    surfaces as the backend's return value."""
    import hashlib
    digest = hashlib.sha256(body).hexdigest()
    with Session(_engine) as session:
        existing = session.get(SubmissionBlob, (digest, submission_handle))
        if existing is None:
            session.add(SubmissionBlob(
                hash=digest,
                submission_handle=submission_handle,
                content_type=content_type,
                size=len(body),
                data=body,
            ))
            session.commit()
    return {
        "kind": "blob",
        "hash": digest,
        "content_type": content_type,
        "size": len(body),
    }


def get_submission_blob(
    *, submission_handle: str, blob_hash: str,
) -> Optional[tuple[bytes, str]]:
    """Fetch a blob's bytes and content type. Returns None when the
    blob doesn't exist or doesn't belong to this submission."""
    with Session(_engine) as session:
        row = session.get(SubmissionBlob, (blob_hash, submission_handle))
        if row is None:
            return None
        return row.data, row.content_type


def init_db() -> None:
    """Create the schema if it doesn't exist. Called once at startup."""
    Base.metadata.create_all(_engine)
    # Crash-recovery first, then the schema migrations. The recovery
    # restores data left behind by an earlier migration that crashed
    # mid-flight (pysqlite auto-commits before each DDL, so a failed
    # `CREATE INDEX` doesn't roll back the prior `RENAME` + `CREATE
    # TABLE` — leaving form_version empty and the data stranded in
    # `_form_version_pre_minor`). The check is cheap (one
    # `SELECT name FROM sqlite_master`) on every startup; it only
    # touches data when the orphan table is present.
    _recover_from_partial_uq_migration()
    _migrate_add_columns()
    _heal_orphaned_step_versions()
    _ensure_reporting_views()


# The flattened view BI tools read. Named here rather than in the
# Superset module because it is a property of frontflow's schema, and
# keeping it beside the models is what stops it drifting from them.
REPORTING_VIEW = "v_frontflow_submissions"


def _ensure_reporting_views() -> None:
    """(Re)create the flattened reporting view.

    One row per step, carrying its submission and form context, so a BI
    tool can chart submissions without knowing frontflow's normalisation.
    `form_values` is exposed as JSONB on Postgres. The underlying column
    is plain `json` (SQLAlchemy's JSON type), and Postgres `json` has no
    equality operator — so any GROUP BY on it fails with "could not
    identify an equality operator for type json". Superset puts every
    column in Dimensions by default, which means a table chart over this
    view errors on sight. Casting to jsonb here fixes that without
    touching frontflow's own storage, and keeps per-form fields reachable
    as `form_values->>'region'` with no schema change when a form gains a
    field.

    DROP + CREATE rather than CREATE OR REPLACE: Postgres only lets
    REPLACE append columns to the end of a view, so any reordering or
    rename fails with "cannot change name of view column". Dropping
    first makes the definition here authoritative.

    Soft-deleted submissions are excluded, matching every user-facing
    read path — a tombstoned submission that still appeared in charts
    would be a surprising leak.
    """
    # Postgres-only cast; SQLite has no jsonb and needs none, having no
    # equality problem to solve.
    form_values_expr = (
        "st.form_values::jsonb"
        if _engine.dialect.name == "postgresql"
        else "st.form_values"
    )

    statements = [
        f"DROP VIEW IF EXISTS {REPORTING_VIEW}",
        f"""
        CREATE VIEW {REPORTING_VIEW} AS
        SELECT
            s.submission_id      AS submission_id,
            s.handle             AS submission_handle,
            s.state              AS submission_state,
            s.created_at         AS created_at,
            s.updated_at         AS updated_at,
            s.terminated_at      AS terminated_at,
            f.form_id            AS form_id,
            f.name               AS form_name,
            fv.version           AS form_version,
            fv.minor_version     AS form_minor_version,
            st.node_id           AS node_id,
            st.seq               AS step_seq,
            st.kind              AS step_kind,
            st.state             AS step_state,
            st.started_at        AS step_started_at,
            st.submitted_at      AS step_submitted_at,
            st.button_clicked    AS button_clicked,
            st.user_id           AS user_id,
            {form_values_expr}   AS form_values
        FROM submission s
        JOIN form_version fv ON fv.id = s.form_version_id
        JOIN form f          ON f.form_id = fv.form_id
        LEFT JOIN step st    ON st.submission_handle = s.handle
        WHERE s.deleted_at IS NULL
          -- Control-panel sessions are excluded. A panel is a working
          -- surface: its "answers" are filter settings, not data. Left
          -- in, they land in the same form_values column real
          -- submissions use, and one form's control values start
          -- colliding with another form's data -- which is exactly how
          -- a filter widget named `units` put objects into a column of
          -- numbers and broke every chart reading it.
          -- NULL is a row written before the column existed: a real
          -- submission.
          AND (s.kind IS NULL OR s.kind = 'submission')
        """,
    ]

    with _engine.begin() as conn:
        for statement in statements:
            conn.exec_driver_sql(statement)

    _grant_reporting_view_to_superset_ro()


def _grant_reporting_view_to_superset_ro() -> None:
    """Give the read-only Superset role the reporting view and nothing else.

    Postgres only, and only when the role exists — an install that never
    set one up (or any SQLite install) simply skips it. Guarded rather
    than attempted-and-caught so a genuine permissions problem is not
    swallowed alongside "no such role".

    The REVOKE is the security-load-bearing half, and it runs on every
    startup because early installs were provisioned with
    ``GRANT SELECT ON ALL TABLES`` plus a default-privileges rule for
    future tables. The init script that did so only runs when the data
    volume is first created, so fixing it there repairs new installs and
    leaves existing ones wide open. Repairing it here reaches both.

    Why it matters: Superset's row-level security resolves predicates per
    *dataset*. A query naming a table that has no dataset gets no
    predicate injected — so a broad grant does not merely over-expose the
    auth tables (``app_user.password_hash``, ``app_session.token``), it
    silently defeats RLS for anyone able to write SQL. Narrowing the
    grant to the views is what makes RLS an actual boundary rather than a
    convention, and it is a precondition for handing anyone SQL Lab.
    """
    if _engine.dialect.name != "postgresql":
        return

    try:
        with _engine.begin() as conn:
            conn.exec_driver_sql(
                f"""
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM pg_roles WHERE rolname = 'superset_ro'
                    ) THEN
                        -- Drop any blanket access first, including the rule
                        -- that would re-grant it on every table created later.
                        REVOKE SELECT ON ALL TABLES IN SCHEMA public
                            FROM superset_ro;
                        ALTER DEFAULT PRIVILEGES IN SCHEMA public
                            REVOKE SELECT ON TABLES FROM superset_ro;

                        -- Then hand back exactly the reporting surface.
                        GRANT SELECT ON {REPORTING_VIEW} TO superset_ro;
                    END IF;
                END
                $$
                """
            )
    except Exception as exc:  # noqa: BLE001 - non-fatal
        # A frontflow user without GRANT rights should still start.
        import logging

        logging.getLogger("frontflow").warning(
            "Could not grant SELECT on %s to superset_ro: %s",
            REPORTING_VIEW,
            exc,
        )


def _dedupe_step_rows_in_place() -> int:
    """Collapse Step rows that share `(submission_handle,
    form_version_id, node_id)` down to a single canonical row.

    A separate pass from the orphan-version cleanup below: those two
    bugs accumulated independently. The pre-fix
    `_heal_orphaned_step_versions` moved orphan rows onto the
    submission's pin WITHOUT first checking whether canonical rows
    already lived there — so submissions that had been hit by both
    the buggy auto-repin AND the buggy heal ended up with multiple
    rows per (handle, pin, node_id). After hydration the runtime saw
    those as legitimate chain entries and `sync_submission` then
    re-persisted them with FRESHLY-ASSIGNED seq numbers (the writer
    uses `enumerate(submission.steps)`), ossifying the duplication
    in a form where a `(handle, fv, seq)` grouping would no longer
    see them as duplicates. Grouping by `node_id` instead catches
    both shapes — the original same-seq dupes AND the re-keyed
    same-node-id dupes.

    This is safe because the runtime maintains the invariant that
    each node_id appears at most once in `submission.steps` per
    submission per active form_version:
      - `clear_submission_from` with mode='edit' re-opens a node in
        place rather than appending a fresh entry.
      - The advance loop's `if submission.steps[nxt_idx].node_id ==
        latest.next_node_id: continue` guard prevents a re-route
        from appending a duplicate.
    Multiple rows with the same node_id therefore always represent
    corruption, not a legitimate workflow shape.

    Keeper rule (in order):
      1. Prefer an unsubmitted row (submitted_at IS NULL) — that's
         the active draft a user is currently filling. Surviving
         a submitted dupe here would lose the user's in-progress
         work.
      2. Among submitted rows: latest `submitted_at` wins. The
         most recent successful sync_submission reflects the
         runtime's actual chain state.
      3. Ties break on `started_at` desc, then `id` desc.

    Returns the number of duplicate rows deleted. Cheap when there
    are no duplicates: the GROUP BY ... HAVING ... returns zero
    rows and we early-return.
    """
    with Session(_engine) as session:
        # Find every (handle, fvid, node_id) group with > 1 row.
        groups = session.execute(
            select(
                Step.submission_handle,
                Step.form_version_id,
                Step.node_id,
            )
            .group_by(
                Step.submission_handle,
                Step.form_version_id,
                Step.node_id,
            )
            .having(func.count(Step.id) > 1)
        ).all()
        if not groups:
            return 0
        total_deleted = 0
        for handle, fvid, node_id in groups:
            row_ids = session.execute(
                select(Step.id)
                .where(
                    Step.submission_handle == handle,
                    Step.form_version_id == fvid,
                    Step.node_id == node_id,
                )
                .order_by(
                    # (1) Active draft (NULL submitted_at) wins.
                    # `is_(None).desc()` sorts True (NULL) before
                    # False (non-NULL).
                    Step.submitted_at.is_(None).desc(),
                    # (2) Then most-recently-submitted.
                    Step.submitted_at.desc().nulls_last(),
                    # (3) Then most-recently-started (tiebreak when
                    # multiple rows share a submitted_at, or none
                    # have one).
                    Step.started_at.desc().nulls_last(),
                    # (4) Final tiebreak: highest id wins (the most
                    # recently inserted row, which is by far the most
                    # common shape after a `sync_submission` run).
                    Step.id.desc(),
                )
            ).scalars().all()
            keep_id = row_ids[0]
            del keep_id  # kept for clarity; not used directly below
            drop_ids = list(row_ids[1:])
            if drop_ids:
                result = session.execute(
                    delete(Step).where(Step.id.in_(drop_ids))
                )
                total_deleted += result.rowcount or 0
        session.commit()
        return total_deleted


def _heal_orphaned_step_versions() -> None:
    """Reconcile Step rows whose `form_version_id` doesn't match
    their submission's current pin, when the two are minor siblings
    of the same major.

    Two related bugs produced these orphans, both pre-fix:

    1. `auto_repin_minor_submissions` updated `Submission.
       form_version_id` but left the Step rows on the prior minor.
       Consequence: `list_submission_versions` (which joins through
       Step) returned only the old minor; the detail builder's
       default `viewing_version_id = pin` failed the membership
       check and 404'd "this submission has no data on
       form_version_id N".

    2. After (1), `sync_submission` wrote a NEW set of Step rows on
       the new pin without clearing the orphans. After several
       minor bumps the submission accumulated multiple parallel
       copies of its chain — one per minor it had lived under —
       and the summary view rendered every duplicate.

    The reconciliation is per-submission:

      - **Canonical pin (the common case)**: if any Step rows exist
        on the submission's current pin, those are authoritative —
        the orphans are stale duplicates left by an old auto-repin
        cycle. DELETE the orphan rows.

      - **Empty pin (the rare case)**: if the pin has no Step rows
        (auto-repin happened, sync_submission hasn't run since),
        the orphans are the only chain data. Promote the MOST
        RECENT minor sibling's rows to the pin and delete the
        rest. "Most recent" wins because that's where the runtime
        was last writing through sync_submission.

    Cross-major orphans (Step rows on a different `version`, not
    just a different `minor_version`) are left strictly alone —
    those are legitimate frozen-history chains from a force-repin
    and must stay viewable via the version picker as read-only
    history.

    Event rows are NOT touched. An event's `form_version_id` is
    historical: "this happened when the form was at version V".
    Moving it would misrepresent when the action occurred. The
    FK column is nullable, indexed, and points at a row that still
    exists (we never delete form_version rows), so stale-from-pin
    references are harmless.

    Cheap on a clean DB: the WHERE clauses return zero rows when no
    work is needed; both passes early-return without writing.
    """
    # First pass: collapse multiple Step rows on the same (handle,
    # form_version_id, seq) tuple down to one. This is independent
    # of the orphan-version logic below — see the function's
    # docstring for the bug history that necessitates it. Runs
    # first so the orphan pass operates on a clean per-(handle, fv,
    # seq) row set.
    dedup_deleted = _dedupe_step_rows_in_place()

    with Session(_engine) as session:
        sp = FormVersion.__table__.alias("sp")  # submission's pin
        st = FormVersion.__table__.alias("st")  # step's version
        # Every (submission, orphan-minor) pair where step rows
        # live on a minor sibling of the pin. DISTINCT collapses
        # multiple step rows on the same orphan minor to one entry
        # — the per-submission decision is the same for all rows
        # on the same orphan.
        orphan_pairs = session.execute(
            select(
                Submission.handle,
                Submission.form_version_id,  # pin
                Step.form_version_id,        # orphan minor
                st.c.minor_version,
            )
            .join(Submission, Submission.handle == Step.submission_handle)
            .join(sp, sp.c.id == Submission.form_version_id)
            .join(st, st.c.id == Step.form_version_id)
            .where(
                Step.form_version_id != Submission.form_version_id,
                sp.c.form_id == st.c.form_id,
                sp.c.version == st.c.version,
            )
            .distinct()
        ).all()
        if not orphan_pairs:
            # Nothing to reconcile across versions; the first pass
            # may still have cleaned up same-version duplicates,
            # which is worth logging on its own.
            if dedup_deleted:
                print(
                    f"[migrate] deduplicated {dedup_deleted} stale "
                    "step row(s) from an earlier buggy heal cycle"
                )
            return

        # Group by submission so the per-row decision (delete vs
        # move) is made once with full information for that
        # submission — we don't want to move and then immediately
        # delete because a later iteration discovered canonical
        # rows on the pin.
        per_submission: dict[str, dict[str, Any]] = {}
        for handle, pin, orphan_fvid, orphan_minor in orphan_pairs:
            entry = per_submission.setdefault(
                handle, {"pin": pin, "orphans": []}
            )
            entry["orphans"].append(
                {"fvid": orphan_fvid, "minor": orphan_minor}
            )

        deleted_rows = 0
        moved_rows = 0
        for handle, info in per_submission.items():
            pin = info["pin"]
            orphans = info["orphans"]
            canonical_count = session.scalar(
                select(func.count(Step.id)).where(
                    Step.submission_handle == handle,
                    Step.form_version_id == pin,
                )
            ) or 0
            orphan_fvids = [o["fvid"] for o in orphans]

            if canonical_count > 0:
                # Pin is authoritative — orphans are stale dupes.
                result = session.execute(
                    delete(Step).where(
                        Step.submission_handle == handle,
                        Step.form_version_id.in_(orphan_fvids),
                    )
                )
                deleted_rows += result.rowcount or 0
            else:
                # Pin is empty — promote the most recent minor's
                # rows. The "most recent" tiebreaker matches the
                # runtime's writer behavior: sync_submission writes
                # to whatever pin was current at the time, so the
                # newest minor reflects the latest known chain
                # state. Older minors are stale snapshots.
                orphans.sort(key=lambda o: o["minor"], reverse=True)
                keep_fvid = orphans[0]["fvid"]
                discard_fvids = [o["fvid"] for o in orphans[1:]]

                move_result = session.execute(
                    update(Step)
                    .where(
                        Step.submission_handle == handle,
                        Step.form_version_id == keep_fvid,
                    )
                    .values(form_version_id=pin)
                )
                moved_rows += move_result.rowcount or 0

                if discard_fvids:
                    drop_result = session.execute(
                        delete(Step).where(
                            Step.submission_handle == handle,
                            Step.form_version_id.in_(discard_fvids),
                        )
                    )
                    deleted_rows += drop_result.rowcount or 0

        session.commit()
        print(
            f"[migrate] healed step rows: "
            f"dedup={dedup_deleted}, moved={moved_rows}, "
            f"orphan-deleted={deleted_rows}"
        )


def _recover_from_partial_uq_migration() -> None:
    """Restore data left in `_form_version_pre_minor` by a crashed
    constraint-swap migration. The crash left the database with:
      - `form_version` exists, possibly empty or populated by a
        post-crash `scan_workflows()` that re-upserted to the empty
        table (with new ids that don't match what submissions
        reference);
      - `_form_version_pre_minor` exists with the original rows and
        their original ids.

    Existing submissions reference the original ids, which now live
    only in the orphan table. The fix: copy those rows back to
    `form_version` (deleting any post-crash rows that conflict), then
    drop the orphan. After this runs, `scan_workflows()` will rescan
    naturally and either find a hash match (no-op) or insert a new
    minor row.
    """
    inspector = inspect(_engine)
    tables = set(inspector.get_table_names())
    if "_form_version_pre_minor" not in tables:
        return

    with _engine.begin() as conn:
        old_rows = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM _form_version_pre_minor"
        ).scalar() or 0
        new_rows = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM form_version"
        ).scalar() or 0

        if new_rows > 0:
            # form_version has rows that were created after the crash
            # (almost certainly by scan_workflows running once on the
            # empty table). Those rows have ids assigned by SQLite's
            # AUTOINCREMENT and aren't tied to existing submissions.
            # Wipe them so we can restore the originals at their
            # original ids without UNIQUE-key collisions.
            print(
                f"[migrate] recovering form_version: replacing "
                f"{new_rows} post-crash row(s) with {old_rows} "
                f"preserved row(s) from the previous migration"
            )
            conn.exec_driver_sql("DELETE FROM form_version")
        else:
            print(
                f"[migrate] recovering form_version: restoring "
                f"{old_rows} row(s) preserved from a previous "
                "interrupted migration"
            )

        # The orphan table was created from the pre-migration schema
        # PLUS the `minor_version` column added in the same scan, so
        # column names line up.
        conn.exec_driver_sql(
            "INSERT INTO form_version ("
            "  id, form_id, version, minor_version, content_hash, "
            "  compiled_graph, source, created_at"
            ") SELECT "
            "  id, form_id, version, minor_version, content_hash, "
            "  compiled_graph, source, created_at "
            "FROM _form_version_pre_minor"
        )
        conn.exec_driver_sql("DROP TABLE _form_version_pre_minor")


def _migrate_form_version_uq(inspector) -> None:
    """Replace the old `(form_id, content_hash)` unique constraint on
    form_version with the new `(form_id, version, minor_version)`
    one. Idempotent: detects whether each end is already in place
    and only touches what needs changing. Dialect-aware (Postgres
    can drop a constraint by name; SQLite needs a table rebuild).
    """
    dialect = _engine.dialect.name
    uqs = inspector.get_unique_constraints("form_version")

    has_old_uq = any(
        set(uq["column_names"]) == {"form_id", "content_hash"}
        for uq in uqs
    )
    has_new_uq = any(
        set(uq["column_names"]) == {
            "form_id", "version", "minor_version",
        }
        for uq in uqs
    )

    if not has_old_uq and has_new_uq:
        return  # nothing to do

    if dialect == "postgresql":
        if has_old_uq:
            old_uq = next(
                uq for uq in uqs
                if set(uq["column_names"]) == {"form_id", "content_hash"}
            )
            name = old_uq["name"]
            if name:
                with _engine.begin() as conn:
                    conn.exec_driver_sql(
                        f'ALTER TABLE form_version '
                        f'DROP CONSTRAINT "{name}"'
                    )
        if not has_new_uq:
            # Postgres syntax — ADD CONSTRAINT ... UNIQUE (cols).
            # Naming explicitly so subsequent inspections find it
            # predictably across deployments.
            with _engine.begin() as conn:
                conn.exec_driver_sql(
                    "ALTER TABLE form_version "
                    "ADD CONSTRAINT uq_form_version_major_minor "
                    "UNIQUE (form_id, version, minor_version)"
                )
        return

    if dialect == "sqlite":
        # SQLite can't drop or add constraints on an existing table —
        # the only way is to recreate. Move the old table aside,
        # let SQLAlchemy emit the new definition via `create_all`
        # against a temporary metadata, copy rows over, drop the
        # old. We rely on the fact that no other transaction is
        # mid-flight; this runs at startup before the app accepts
        # requests.
        #
        # SQLite preserves indexes through ALTER TABLE RENAME — they
        # stay attached to the renamed table but keep their original
        # names. That makes the subsequent `FormVersion.__table__
        # .create(conn)` collide on `CREATE INDEX ix_form_version_
        # form_id`. Drop the indexes (by their declared names)
        # before the rename so the fresh table's indexes can be
        # created cleanly. The temp table doesn't need indexes — it
        # exists for one INSERT SELECT and is dropped immediately.
        existing_indexes = [
            idx["name"] for idx in inspector.get_indexes("form_version")
        ]
        with _engine.begin() as conn:
            for idx_name in existing_indexes:
                # Quote to handle any names with hyphens / case.
                conn.exec_driver_sql(
                    f'DROP INDEX IF EXISTS "{idx_name}"'
                )
            conn.exec_driver_sql(
                "ALTER TABLE form_version RENAME TO _form_version_pre_minor"
            )
            # Recreate from the live model — picks up the new
            # constraint set automatically.
            FormVersion.__table__.create(conn)
            conn.exec_driver_sql(
                "INSERT INTO form_version ("
                "  id, form_id, version, minor_version, content_hash, "
                "  compiled_graph, source, created_at"
                ") SELECT "
                "  id, form_id, version, minor_version, content_hash, "
                "  compiled_graph, source, created_at "
                "FROM _form_version_pre_minor"
            )
            conn.exec_driver_sql("DROP TABLE _form_version_pre_minor")
        return

    # Other dialects — leave alone; admin can drop the old constraint
    # manually. The application logic still prevents duplicate inserts
    # via the "is the latest source already this?" check in
    # `upsert_form_version`, so the old constraint just bites on minor
    # bumps.


def _timestamp_ddl() -> str:
    """DDL type name for a timestamp column, rendered for the engine's
    dialect: `DATETIME` on SQLite, `TIMESTAMP WITHOUT TIME ZONE` on
    PostgreSQL. Hardcoding `DATETIME` crashes Postgres at startup with
    `type "datetime" does not exist`, so every ALTER that adds a
    timestamp column must go through here."""
    return DateTime().compile(dialect=_engine.dialect)


def _migrate_add_columns() -> None:
    """Lightweight migration — `create_all` adds missing tables but not
    missing columns on existing ones. Add columns introduced after a DB
    was first created. Uses SQLAlchemy's inspector so it works on both
    SQLite and Postgres."""
    inspector = inspect(_engine)
    ts = _timestamp_ddl()

    if "app_workspace_acl" in set(inspector.get_table_names()):
        acl_cols = {
            c["name"] for c in inspector.get_columns("app_workspace_acl")
        }
        if "role" not in acl_cols:
            with _engine.begin() as conn:
                conn.exec_driver_sql(
                    "ALTER TABLE app_workspace_acl ADD COLUMN role VARCHAR"
                )
                # Existing grants keep behaving as before — view only.
                conn.exec_driver_sql(
                    "UPDATE app_workspace_acl SET role = 'view' "
                    "WHERE role IS NULL"
                )

    form_cols = {c["name"] for c in inspector.get_columns("form")}
    if "theme" not in form_cols:
        # JSON renders to the right column type per dialect.
        with _engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE form ADD COLUMN theme JSON"
            )
    if "visibility" not in form_cols:
        with _engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE form ADD COLUMN visibility VARCHAR"
            )
            # Existing forms keep behaving as before — public.
            conn.exec_driver_sql(
                "UPDATE form SET visibility = 'public' "
                "WHERE visibility IS NULL"
            )
    if "unlisted_token" not in form_cols:
        with _engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE form ADD COLUMN unlisted_token VARCHAR"
            )

    sub_cols = {c["name"] for c in inspector.get_columns("submission")}
    if "kind" not in sub_cols:
        with _engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE submission ADD COLUMN kind VARCHAR"
            )
            # Every existing row is a real submission — control panels
            # did not exist when they were written.
            conn.exec_driver_sql(
                "UPDATE submission SET kind = 'submission' "
                "WHERE kind IS NULL"
            )
    if "updated_at" not in sub_cols:
        with _engine.begin() as conn:
            conn.exec_driver_sql(
                f"ALTER TABLE submission ADD COLUMN updated_at {ts}"
            )
            # Backfill existing rows: best-known update time is when the
            # submission terminated, else when it was created.
            conn.exec_driver_sql(
                "UPDATE submission "
                "SET updated_at = COALESCE(terminated_at, created_at) "
                "WHERE updated_at IS NULL"
            )
    if "cleared_run_ids" not in sub_cols:
        with _engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE submission ADD COLUMN cleared_run_ids JSON"
            )
    if "parent_submission_handle" not in sub_cols:
        with _engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE submission "
                "ADD COLUMN parent_submission_handle VARCHAR"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS "
                "ix_submission_parent_handle "
                "ON submission (parent_submission_handle)"
            )
    if "parent_assign_node_id" not in sub_cols:
        with _engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE submission "
                "ADD COLUMN parent_assign_node_id VARCHAR"
            )
    if "parent_assign_op_idx" not in sub_cols:
        with _engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE submission "
                "ADD COLUMN parent_assign_op_idx INTEGER"
            )
    if "deleted_at" not in sub_cols:
        with _engine.begin() as conn:
            conn.exec_driver_sql(
                f"ALTER TABLE submission ADD COLUMN deleted_at {ts}"
            )
            # Index it — every user-facing read path filters on this
            # column, so a scan is hot. SQLite uses an INDEX with a
            # WHERE predicate; Postgres would too if we used native
            # DDL, but a plain index is dialect-portable and the
            # selectivity stays good (the deleted set is tiny vs
            # active rows).
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS "
                "ix_submission_deleted_at "
                "ON submission (deleted_at)"
            )

    # Per-step error: introduced to give the chain UI a specific
    # failure message per step (the submission-level error doesn't say
    # *which* step blew up). Pre-existing failed rows have no message
    # — that's fine; the column is nullable and the UI tolerates None.
    step_cols = {c["name"] for c in inspector.get_columns("step")}
    if "error" not in step_cols:
        with _engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE step ADD COLUMN error VARCHAR"
            )
    # Per-step traceback: paired with `error`. Captures
    # traceback.format_exc() at the raise site so the chain UI's
    # collapsible details panel can show the full stack, not just
    # the one-line message.
    if "traceback" not in step_cols:
        with _engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE step ADD COLUMN traceback VARCHAR"
            )

    # Repair: any submission in a terminal state (success/failed) whose
    # `terminated_at` is null is a leftover from an earlier code path
    # that didn't persist the timestamp on every termination route.
    # The completion-time analytics depends on this column, so we
    # backfill best-known values rather than leaving the rows
    # uncountable. Best-known order:
    #   1. The latest step's `submitted_at` (most accurate — that's
    #      when the chain reached the terminal node).
    #   2. The submission's `updated_at` (set on every state change,
    #      so at minimum it's the row's last persist time).
    # Both fallbacks may overshoot the true termination by a few
    # seconds; both are tolerable for analytics. The "real" fix is
    # at write time, which is now reliable per the runtime test
    # suite — this only patches up rows that pre-date that.
    with _engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE submission "
            "SET terminated_at = COALESCE("
            "    (SELECT MAX(submitted_at) FROM step "
            "     WHERE step.submission_handle = submission.handle), "
            "    updated_at"
            ") "
            "WHERE terminated_at IS NULL "
            "AND state IN ('success', 'failed')"
        )

    fv_cols = {c["name"] for c in inspector.get_columns("form_version")}
    if "minor_version" not in fv_cols:
        # New column on form_version: the non-structural (minor) version
        # of a form, bumped when the source text changes but the compiled
        # graph is identical. Existing rows backfill to 0 — they predate
        # the minor concept and represent "the only version at major
        # vN" (no surrounding minors). The unique constraint
        # (form_id, version, minor_version) covers them correctly with
        # this default.
        with _engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE form_version "
                "ADD COLUMN minor_version INTEGER NOT NULL DEFAULT 0"
            )

    # Unique-constraint migration. The original schema enforced
    # (form_id, content_hash) — exactly-one row per compiled graph.
    # The minor-version system intentionally violates that: a minor
    # bump produces a new row sharing the prior row's content_hash
    # but a different source. So the old constraint must be dropped
    # and the new (form_id, version, minor_version) added.
    #
    # `Base.metadata.create_all` doesn't alter existing tables, so
    # we do the swap explicitly here. Without it, upsert_form_version
    # raises an IntegrityError on the first source-only edit and
    # the form falls out of FORM_VERSION_IDS — leaving the Source
    # tab to hit the in-memory fallback path.
    _migrate_form_version_uq(inspector)

    step_cols = {c["name"] for c in inspector.get_columns("step")}
    if "form_version_id" not in step_cols:
        with _engine.begin() as conn:
            # Add nullable first so the ALTER succeeds on existing rows,
            # then backfill from the submission's pin (accurate for any
            # submission that has not been re-pinned — true for all
            # existing data, since version-partitioned steps are new).
            conn.exec_driver_sql(
                "ALTER TABLE step ADD COLUMN form_version_id INTEGER "
                "REFERENCES form_version(id)"
            )
            conn.exec_driver_sql(
                "UPDATE step SET form_version_id = ("
                "  SELECT submission.form_version_id "
                "  FROM submission "
                "  WHERE submission.handle = step.submission_handle"
                ") WHERE form_version_id IS NULL"
            )
    if "external_state" not in step_cols:
        with _engine.begin() as conn:
            # Per-chain-step output. Was an in-memory field on the
            # runtime's StepSubmission for some time before becoming a
            # persisted column — historic rows have no value here and
            # stay null. New writes will populate it. There's no
            # meaningful backfill: the data lived only in the runtime
            # process and was lost on every restart.
            conn.exec_driver_sql(
                "ALTER TABLE step ADD COLUMN external_state JSON"
            )
    if "user_id" not in step_cols:
        with _engine.begin() as conn:
            # Step-level submitter attribution. Drives the
            # per-submission visibility gate (a user with at least
            # one step they submitted on a given submission may view
            # that submission). Legacy rows have no value and stay
            # null — they're invisible to non-admins until an admin
            # backfills, which is the safer default.
            conn.exec_driver_sql(
                "ALTER TABLE step ADD COLUMN user_id INTEGER "
                "REFERENCES app_user(id)"
            )

    event_cols = {c["name"] for c in inspector.get_columns("event")}
    if "form_version_id" not in event_cols:
        with _engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE event ADD COLUMN form_version_id INTEGER "
                "REFERENCES form_version(id)"
            )
            # Backfill from the submission's pin where possible — that
            # is exact for submissions that never re-pinned (every
            # submission today, since this feature is new). After this
            # migration the runtime tags every new event with its
            # current version; events from prior versions, once a
            # force re-pin happens, will be tagged at write-time.
            conn.exec_driver_sql(
                "UPDATE event SET form_version_id = ("
                "  SELECT submission.form_version_id "
                "  FROM submission "
                "  WHERE submission.handle = event.submission_handle"
                ") WHERE form_version_id IS NULL"
            )

    # app_user is created by create_all before this runs, so it always
    # exists here; only post-M1 columns may be missing.
    user_cols = {c["name"] for c in inspector.get_columns("app_user")}
    if "is_active" not in user_cols:
        with _engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE app_user ADD COLUMN is_active BOOLEAN"
            )
            # Existing accounts stay usable.
            conn.exec_driver_sql(
                "UPDATE app_user SET is_active = 1 "
                "WHERE is_active IS NULL"
            )
    if "must_change_password" not in user_cols:
        with _engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE app_user "
                "ADD COLUMN must_change_password BOOLEAN"
            )
            conn.exec_driver_sql(
                "UPDATE app_user SET must_change_password = 0 "
                "WHERE must_change_password IS NULL"
            )
    if "external_id" not in user_cols:
        with _engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE app_user ADD COLUMN external_id VARCHAR"
            )
            # Existing accounts have no external mirror — leave null.
            # Create a partial unique index so multiple null rows are
            # allowed but non-null values must be unique. SQLite + most
            # other dialects treat NULL as not-equal-to-anything, so
            # a plain unique index suffices for the "unique-when-set"
            # semantic; create a regular index to back lookups.
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_app_user_external_id "
                "ON app_user (external_id)"
            )
    if "notification_preferences" not in user_cols:
        with _engine.begin() as conn:
            # JSON columns store as text on SQLite; declare TEXT for
            # portability and let SQLAlchemy serialize on writes.
            conn.exec_driver_sql(
                "ALTER TABLE app_user "
                "ADD COLUMN notification_preferences TEXT"
            )
            # Existing rows get an empty preferences dict — handlers
            # default to "send" when a key is absent.
            conn.exec_driver_sql(
                "UPDATE app_user SET notification_preferences = '{}' "
                "WHERE notification_preferences IS NULL"
            )


# --- Form theme ------------------------------------------------------------


def get_form_theme(form_id: str) -> Optional[dict[str, Any]]:
    """A form's stored per-form theme, or None when uncustomized."""
    with Session(_engine) as session:
        form = session.get(Form, form_id)
        return form.theme if form is not None else None


def form_folder(form_id: str) -> Optional[str]:
    """A form's folder path ("" for top-level), or None if no such
    form. Used by the authorization layer to resolve folder grants."""
    with Session(_engine) as session:
        form = session.get(Form, form_id)
        return form.folder_path if form is not None else None


def set_form_theme(form_id: str, theme: Optional[dict[str, Any]]) -> bool:
    """Persist a form's theme (or clear it with None). Returns False if
    the form doesn't exist."""
    with Session(_engine) as session:
        form = session.get(Form, form_id)
        if form is None:
            return False
        form.theme = theme
        form.updated_at = _utcnow()
        session.commit()
        return True


# --- Comments --------------------------------------------------------------


def list_comments(
    form_id: str, submission_handle: str, thread_id: str,
) -> list[dict[str, Any]]:
    """A thread's comments, oldest first."""
    with Session(_engine) as session:
        rows = (
            session.query(Comment)
            .filter_by(
                form_id=form_id,
                submission_handle=submission_handle,
                thread_id=thread_id,
            )
            .order_by(Comment.created_at, Comment.id)
            .all()
        )
        return [
            {
                "id": c.id,
                "thread_id": c.thread_id,
                "author": c.author,
                "user_id": c.user_id,
                "body": c.body,
                "created_at": c.created_at,
            }
            for c in rows
        ]


def add_comment(
    form_id: str,
    submission_handle: str,
    thread_id: str,
    author: str,
    body: str,
    user_id: Optional[str] = None,
) -> dict[str, Any]:
    """Append a comment; returns the stored row."""
    with Session(_engine) as session:
        c = Comment(
            form_id=form_id,
            submission_handle=submission_handle,
            thread_id=thread_id,
            author=author,
            user_id=user_id,
            body=body,
            created_at=_utcnow(),
        )
        session.add(c)
        session.commit()
        return {
            "id": c.id,
            "thread_id": c.thread_id,
            "author": c.author,
            "user_id": c.user_id,
            "body": c.body,
            "created_at": c.created_at,
        }


# --- Connections -----------------------------------------------------------


def list_connections() -> list[dict[str, Any]]:
    """All stored connections as metadata. Never includes the secret —
    this is what the console and API surface."""
    with Session(_engine) as session:
        rows = session.scalars(
            select(Connection).order_by(Connection.name)
        ).all()
        return [
            {
                "name": c.name,
                "conn_type": c.conn_type,
                "base_url": c.base_url,
                "auth_kind": c.auth_kind,
                "created_at": _aware(c.created_at),
                "updated_at": _aware(c.updated_at),
            }
            for c in rows
        ]


def get_connection(name: str) -> Optional[dict[str, Any]]:
    """One connection including its decrypted credential payload — for
    the Airflow hook to authenticate with. Returns None if absent.

    Callers must treat the `secret` field as sensitive: it is never
    serialized back over the API."""
    with Session(_engine) as session:
        c = session.get(Connection, name)
        if c is None:
            return None
        return {
            "name": c.name,
            "conn_type": c.conn_type,
            "base_url": c.base_url,
            "auth_kind": c.auth_kind,
            "secret": decrypt_secret(c.secret),
            "created_at": _aware(c.created_at),
            "updated_at": _aware(c.updated_at),
        }



def connection_exists(name: str) -> bool:
    with Session(_engine) as session:
        return session.get(Connection, name) is not None


def upsert_connection(
    *,
    name: str,
    conn_type: str,
    base_url: str,
    auth_kind: str,
    secret: Optional[dict[str, Any]],
) -> None:
    """Create or update a connection. On update, a `secret` of None keeps
    the existing encrypted credentials — so the editor can save metadata
    changes without re-entering a password. Creating a connection with no
    secret is an error."""
    with Session(_engine) as session:
        c = session.get(Connection, name)
        now = _utcnow()
        if c is None:
            if secret is None:
                raise ValueError("a new connection requires credentials")
            session.add(
                Connection(
                    name=name,
                    conn_type=conn_type,
                    base_url=base_url,
                    auth_kind=auth_kind,
                    secret=encrypt_secret(secret),
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            c.conn_type = conn_type
            c.base_url = base_url
            c.auth_kind = auth_kind
            if secret is not None:
                c.secret = encrypt_secret(secret)
            c.updated_at = now
        session.commit()


def delete_connection(name: str) -> bool:
    """Remove a connection. Returns False if it didn't exist."""
    with Session(_engine) as session:
        c = session.get(Connection, name)
        if c is None:
            return False
        session.delete(c)
        session.commit()
        return True


# --- Workspaces ------------------------------------------------------------


def upsert_workspace(
    *, workspace_id: str, name: str, private: bool
) -> dict[str, Any]:
    """Register or refresh a workspace from the DSL.

    `private` sets visibility only when the row is FIRST created. After
    that an admin owns it through the API — a re-scan must not silently
    re-restrict (or re-publish) a workspace someone deliberately changed,
    which is the same rule forms follow.
    """
    with Session(_engine) as session:
        ws = session.get(Workspace, workspace_id)
        now = _utcnow()
        if ws is None:
            ws = Workspace(
                workspace_id=workspace_id,
                name=name,
                visibility="restricted" if private else "public",
                is_live=True,
                created_at=now,
                updated_at=now,
            )
            session.add(ws)
        else:
            ws.name = name
            ws.is_live = True
            ws.updated_at = now
        session.commit()
        session.refresh(ws)
        return _workspace_dict(ws)


def _workspace_dict(ws: "Workspace") -> dict[str, Any]:
    return {
        "workspace_id": ws.workspace_id,
        "name": ws.name,
        "visibility": ws.visibility,
        "unlisted_token": ws.unlisted_token,
        "is_live": ws.is_live,
        "created_at": _aware(ws.created_at),
        "updated_at": _aware(ws.updated_at),
    }


def get_workspace(workspace_id: str) -> Optional[dict[str, Any]]:
    with Session(_engine) as session:
        ws = session.get(Workspace, workspace_id)
        return None if ws is None else _workspace_dict(ws)


def list_workspaces() -> list[dict[str, Any]]:
    with Session(_engine) as session:
        rows = session.scalars(
            select(Workspace).order_by(Workspace.workspace_id)
        ).all()
        return [_workspace_dict(w) for w in rows]


def set_workspace_visibility(
    workspace_id: str, visibility: str
) -> Optional[dict[str, Any]]:
    if visibility not in ("public", "unlisted", "restricted"):
        raise ValueError(
            "visibility must be 'public', 'unlisted', or 'restricted'"
        )
    with Session(_engine) as session:
        ws = session.get(Workspace, workspace_id)
        if ws is None:
            return None
        ws.visibility = visibility
        if visibility == "unlisted" and not ws.unlisted_token:
            ws.unlisted_token = secrets.token_urlsafe(24)
        ws.updated_at = _utcnow()
        session.commit()
        session.refresh(ws)
        return _workspace_dict(ws)


def mark_workspaces_not_live(live_ids: set[str]) -> None:
    """Flag workspaces whose DSL file vanished from the latest scan.

    Rows are kept, not deleted: an ACL and a deliberate visibility change
    should survive a file being moved or briefly removed.
    """
    with Session(_engine) as session:
        for ws in session.scalars(select(Workspace)).all():
            ws.is_live = ws.workspace_id in live_ids
        session.commit()


def workspace_acl_user_ids(workspace_id: str) -> set[int]:
    with Session(_engine) as session:
        rows = session.scalars(
            select(WorkspaceAcl.user_id).where(
                WorkspaceAcl.workspace_id == workspace_id
            )
        ).all()
        return set(rows)


def grant_workspace_access(
    workspace_id: str, user_id: int, role: str = "view"
) -> None:
    if role not in ("view", "manage"):
        raise ValueError("role must be 'view' or 'manage'")
    with Session(_engine) as session:
        row = session.get(WorkspaceAcl, (workspace_id, user_id))
        if row is None:
            session.add(
                WorkspaceAcl(
                    workspace_id=workspace_id, user_id=user_id, role=role
                )
            )
        else:
            row.role = role
        session.commit()


def workspace_acl_role(workspace_id: str, user_id: int) -> Optional[str]:
    """The role a user holds on a workspace, or None."""
    with Session(_engine) as session:
        row = session.get(WorkspaceAcl, (workspace_id, user_id))
        return None if row is None else (row.role or "view")


def revoke_workspace_access(workspace_id: str, user_id: int) -> bool:
    with Session(_engine) as session:
        row = session.get(WorkspaceAcl, (workspace_id, user_id))
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True


# --- Dashboard bindings ----------------------------------------------------


def _binding_dict(b: "DashboardBinding") -> dict[str, Any]:
    return {
        "name": b.name,
        "connection_name": b.connection_name,
        "superset_dashboard_id": b.superset_dashboard_id,
        "embed_uuid": b.embed_uuid,
        "filter_id": b.filter_id,
        "auto_created": b.auto_created,
        "created_at": _aware(b.created_at),
        "updated_at": _aware(b.updated_at),
    }


def list_dashboard_bindings() -> list[dict[str, Any]]:
    """Every known dashboard binding, name-ordered."""
    with Session(_engine) as session:
        rows = session.scalars(
            select(DashboardBinding).order_by(DashboardBinding.name)
        ).all()
        return [_binding_dict(b) for b in rows]


def get_dashboard_binding(name: str) -> Optional[dict[str, Any]]:
    """One binding by its logical name. None if the name is unknown —
    the caller decides whether that means "provision it" or "author
    error"."""
    with Session(_engine) as session:
        b = session.get(DashboardBinding, name)
        return None if b is None else _binding_dict(b)


def upsert_dashboard_binding(
    *,
    name: str,
    connection_name: str,
    superset_dashboard_id: Optional[str] = None,
    embed_uuid: Optional[str] = None,
    filter_id: Optional[str] = None,
    auto_created: Optional[bool] = None,
) -> dict[str, Any]:
    """Create or update a binding.

    On update, a field left as None is *kept*, not cleared. Provisioning
    fills these in over several steps and may only manage some of them
    on a given pass (Superset unreachable, dataset missing, and so on),
    so a partial update must not wipe what an earlier pass established.
    """
    with Session(_engine) as session:
        b = session.get(DashboardBinding, name)
        now = _utcnow()
        if b is None:
            b = DashboardBinding(
                name=name,
                connection_name=connection_name,
                superset_dashboard_id=superset_dashboard_id,
                embed_uuid=embed_uuid,
                filter_id=filter_id,
                auto_created=bool(auto_created),
                created_at=now,
                updated_at=now,
            )
            session.add(b)
        else:
            b.connection_name = connection_name
            if superset_dashboard_id is not None:
                b.superset_dashboard_id = superset_dashboard_id
            if embed_uuid is not None:
                b.embed_uuid = embed_uuid
            if filter_id is not None:
                b.filter_id = filter_id
            if auto_created is not None:
                b.auto_created = auto_created
            b.updated_at = now
        session.commit()
        session.refresh(b)
        return _binding_dict(b)


def delete_dashboard_binding(name: str) -> bool:
    """Forget a binding. Returns False if it didn't exist.

    Only removes frontflow's record — the dashboard in Superset is left
    alone. Deleting there is a separate, explicit decision (and only
    ever valid for `auto_created` bindings).
    """
    with Session(_engine) as session:
        b = session.get(DashboardBinding, name)
        if b is None:
            return False
        session.delete(b)
        session.commit()
        return True


# --- Variables -------------------------------------------------------------


def list_variables() -> list[dict[str, Any]]:
    """All stored variables as metadata — never includes the value.
    This is what the admin UI surfaces."""
    with Session(_engine) as session:
        rows = session.scalars(
            select(Variable).order_by(Variable.name)
        ).all()
        return [
            {
                "name": v.name,
                "description": v.description,
                "created_at": _aware(v.created_at),
                "updated_at": _aware(v.updated_at),
            }
            for v in rows
        ]


def get_variable(name: str) -> Optional[str]:
    """The decrypted value of one variable, or None if absent.

    Used by the template engine and the `variables.get()` DSL helper.
    Never returned via the public API — values stay server-side."""
    with Session(_engine) as session:
        v = session.get(Variable, name)
        if v is None:
            return None
        payload = decrypt_secret(v.value_encrypted)
        # The on-disk shape is {"value": "<the string>"} — see
        # encrypt_secret's signature, which takes a dict.
        return payload.get("value")


def get_variable_meta(name: str) -> Optional[dict[str, Any]]:
    """One variable's metadata — for the admin edit view. Value not
    included; the editor re-enters it on rotation just like
    connections."""
    with Session(_engine) as session:
        v = session.get(Variable, name)
        if v is None:
            return None
        return {
            "name": v.name,
            "description": v.description,
            "created_at": _aware(v.created_at),
            "updated_at": _aware(v.updated_at),
        }


def upsert_variable(
    *,
    name: str,
    value: Optional[str],
    description: Optional[str] = None,
) -> None:
    """Create or update a variable. On update, a `value` of None keeps
    the existing encrypted value — so the editor can edit the
    description without re-entering the value. Creating a variable
    with no value is an error."""
    with Session(_engine) as session:
        v = session.get(Variable, name)
        now = _utcnow()
        if v is None:
            if value is None:
                raise ValueError("a new variable requires a value")
            session.add(
                Variable(
                    name=name,
                    description=description,
                    value_encrypted=encrypt_secret({"value": value}),
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            v.description = description
            if value is not None:
                v.value_encrypted = encrypt_secret({"value": value})
            v.updated_at = now
        session.commit()


def delete_variable(name: str) -> bool:
    """Remove a variable. Returns False if it didn't exist."""
    with Session(_engine) as session:
        v = session.get(Variable, name)
        if v is None:
            return False
        session.delete(v)
        session.commit()
        return True


def get_all_variables() -> dict[str, str]:
    """Decrypted name→value map for every variable. Used to populate
    the template namespace before rendering — done once per render
    pass so a per-variable decrypt happens at most once."""
    with Session(_engine) as session:
        rows = session.scalars(select(Variable)).all()
        out: dict[str, str] = {}
        for v in rows:
            try:
                payload = decrypt_secret(v.value_encrypted)
                value = payload.get("value")
                if value is not None:
                    out[v.name] = value
            except Exception:  # noqa: BLE001
                # A variable whose decrypt fails (corrupted row,
                # rotated key) is treated as missing — render-side
                # references fall through to empty string. Logging
                # the issue is the admin UI's job.
                continue
        return out


# --- File upload blobs -----------------------------------------------------


def create_upload_blob(
    *,
    token: str,
    form_id: str,
    field_id: str,
    filename: str,
    content_type: str,
    data: bytes,
) -> None:
    """Store the bytes of a transient `File` upload, keyed by token.
    Not yet tied to a submission — that happens when the form is
    submitted."""
    with Session(_engine) as session:
        session.add(
            UploadBlob(
                token=token,
                submission_handle=None,
                form_id=form_id,
                field_id=field_id,
                filename=filename,
                content_type=content_type,
                size=len(data),
                data=data,
            )
        )
        session.commit()


def get_upload_blob(token: str) -> Optional[dict[str, Any]]:
    """Fetch an upload blob — bytes and metadata — by token, or None."""
    with Session(_engine) as session:
        b = session.get(UploadBlob, token)
        if b is None:
            return None
        return {
            "token": b.token,
            "form_id": b.form_id,
            "field_id": b.field_id,
            "filename": b.filename,
            "content_type": b.content_type,
            "size": b.size,
            "data": b.data,
        }


def attach_upload_blobs(
    tokens: list[str], submission_handle: str
) -> None:
    """Tie a set of upload blobs to a submission, so they can be found
    and cleaned up when it terminates."""
    if not tokens:
        return
    with Session(_engine) as session:
        for token in tokens:
            b = session.get(UploadBlob, token)
            if b is not None:
                b.submission_handle = submission_handle
        session.commit()


def delete_submission_upload_blobs(submission_handle: str) -> int:
    """Delete every upload blob tied to a submission — called when it
    terminates. Returns how many were removed."""
    with Session(_engine) as session:
        blobs = session.scalars(
            select(UploadBlob).where(
                UploadBlob.submission_handle == submission_handle
            )
        ).all()
        n = len(blobs)
        for b in blobs:
            session.delete(b)
        session.commit()
        return n


# --- Form / form_version upsert --------------------------------------------


@dataclass(frozen=True)
class FormVersionUpsertResult:
    """What happened on a single `upsert_form_version` call. Returned
    so the caller (typically `scan_workflows`) can react to the kind
    of change — e.g. auto-repin in-flight submissions on a minor
    bump, leave them alone on a major bump.

    `form_version_id` is the row id that's now current (which is the
    same as the previous one when `bump == 'none'`).
    """
    form_version_id: int
    version: int
    minor_version: int
    bump: str  # "none" | "minor" | "major"


def upsert_form_version(
    form_id: str,
    name: str,
    folder_path: str,
    compiled_graph: dict[str, Any],
    content_hash: str,
    source: str,
    dsl_visibility: Optional[str] = None,
) -> FormVersionUpsertResult:
    """Record a form and its current compiled state. The three cases:

      - Compiled graph hash AND source text both match the latest row
        → no-op, return that row's id with bump='none'.
      - Compiled graph hash matches the latest row, source text
        differs → insert a new row with same `version`, bumped
        `minor_version`, return new id with bump='minor'.
      - Compiled graph hash differs → insert a new row with bumped
        `version`, `minor_version=0`, return new id with bump='major'.

    Splitting "any source change" from "structural change" lets the
    Source tab always reflect what's actually running (minor bumps
    invalidate the displayed source) while preserving the existing
    repin/version-picker model for real structural updates.

    `dsl_visibility` is the visibility the form's DSL declares (via
    `@form(private=True)` or similar). When set, it's enforced on
    EVERY scan — the DSL is the source of truth; the admin UI does
    not override settings declared in code. When `None`, the form's
    DSL is silent on visibility and the admin owns the value.

    Behavior on the `form` row:
      - Form row doesn't exist yet → visibility = dsl_visibility
        if set, else "public".
      - Form row exists, dsl_visibility set → overwrite the column
        unconditionally (DSL is source of truth on every scan).
      - Form row exists, dsl_visibility is None → leave the column
        untouched (admin owns it).
    """
    if dsl_visibility is not None and dsl_visibility not in (
        "public", "unlisted", "restricted",
    ):
        raise ValueError(
            f"unknown dsl_visibility {dsl_visibility!r}"
        )
    with Session(_engine) as session:
        form = session.get(Form, form_id)
        now = _utcnow()
        if form is None:
            form = Form(
                form_id=form_id,
                name=name,
                folder_path=folder_path,
                is_live=True,
                visibility=dsl_visibility or "public",
                created_at=now,
                updated_at=now,
            )
            session.add(form)
        else:
            form.name = name
            form.folder_path = folder_path
            form.is_live = True
            form.updated_at = now
            # DSL-declared visibility is enforced on every scan.
            # When silent, leave the admin's value alone.
            if dsl_visibility is not None:
                form.visibility = dsl_visibility

        # The "latest" row is the one with the largest
        # (version, minor_version) tuple — ordered lexicographically.
        latest = session.scalars(
            select(FormVersion)
            .where(FormVersion.form_id == form_id)
            .order_by(
                FormVersion.version.desc(),
                FormVersion.minor_version.desc(),
            )
        ).first()

        if latest is not None and latest.content_hash == content_hash:
            if latest.source == source:
                # True no-op: same structure, same source. The existing
                # row is current; don't insert a new minor.
                session.commit()
                return FormVersionUpsertResult(
                    form_version_id=latest.id,
                    version=latest.version,
                    minor_version=latest.minor_version,
                    bump="none",
                )
            # Same structure, different source → minor bump. Insert a
            # new row that shares the major version but carries the
            # updated source. The Source tab on the FORM page will
            # display the new content; submissions pinned to the old
            # minor still see the old source until repinned.
            new_minor = FormVersion(
                form_id=form_id,
                version=latest.version,
                minor_version=latest.minor_version + 1,
                content_hash=content_hash,
                compiled_graph=compiled_graph,
                source=source,
                created_at=now,
            )
            session.add(new_minor)
            session.commit()
            return FormVersionUpsertResult(
                form_version_id=new_minor.id,
                version=new_minor.version,
                minor_version=new_minor.minor_version,
                bump="minor",
            )

        # Compiled graph differs (or no prior row at all) → major bump.
        # minor_version resets to 0 for the new structural revision.
        new_major = FormVersion(
            form_id=form_id,
            version=(latest.version + 1) if latest is not None else 1,
            minor_version=0,
            content_hash=content_hash,
            compiled_graph=compiled_graph,
            source=source,
            created_at=now,
        )
        session.add(new_major)
        session.commit()
        return FormVersionUpsertResult(
            form_version_id=new_major.id,
            version=new_major.version,
            minor_version=new_major.minor_version,
            bump="major",
        )


def mark_forms_live(live_form_ids: set[str]) -> None:
    """Flag forms whose DSL files are no longer present as not-live, so
    the admin can distinguish current forms from archived ones that
    still hold historical submissions."""
    with Session(_engine) as session:
        for form in session.scalars(select(Form)):
            should_be_live = form.form_id in live_form_ids
            if form.is_live != should_be_live:
                form.is_live = should_be_live
        session.commit()


# Submission states that count as "in flight" for auto-repin purposes.
# Terminal states (success, failed, rejected) are intentionally
# excluded — the version a submission terminated on is part of the
# audit record and never changes after the fact.
_IN_FLIGHT_STATES = ("running", "pending", "draft")


def auto_repin_minor_submissions(
    form_id: str,
    major_version: int,
    new_form_version_id: int,
) -> int:
    """Repin every in-flight submission of `form_id` that's on an
    earlier minor of `major_version` onto `new_form_version_id`.

    Called from `scan_workflows` after a minor bump, when either the
    env default or the form's explicit `auto_repin_minor=True` opts
    in. Same compiled graph means no shape change, so the repin is
    always safe — no `validate_repin` step needed.

    Terminal submissions (success / failed / rejected) are NEVER
    touched, regardless of the auto-repin setting. The version a
    submission finished on is preserved verbatim for historical
    fidelity.

    Records a `submission_auto_repinned` event on each migrated row
    so the submission's History tab shows the transparent migration
    instead of an unexplained version jump.

    Returns the number of submissions migrated.
    """
    with Session(_engine) as session:
        # Find the form_version row ids that belong to earlier minors
        # of this major. (Could go direct from the in-flight submissions
        # to a JOIN on FormVersion, but doing the lookup explicitly keeps
        # the read query simple and indexable.)
        earlier_minors = session.scalars(
            select(FormVersion.id)
            .where(
                FormVersion.form_id == form_id,
                FormVersion.version == major_version,
                FormVersion.id != new_form_version_id,
            )
        ).all()
        if not earlier_minors:
            return 0

        in_flight = session.scalars(
            select(Submission)
            .where(
                Submission.form_version_id.in_(earlier_minors),
                Submission.state.in_(_IN_FLIGHT_STATES),
                # Tombstoned submissions don't get re-pinned —
                # they've been removed from the user-facing world,
                # and changing their pin would muddy the audit
                # trail an undelete would want to restore.
                Submission.deleted_at.is_(None),
            )
        ).all()

        now = _utcnow()
        migrated = 0
        for sub in in_flight:
            from_id = sub.form_version_id
            sub.form_version_id = new_form_version_id
            # Re-point the submission's Step rows from the prior
            # minor to the new one. The compiled graph is identical
            # between sibling minors (that's the definition of a
            # minor bump), so the step shape stays valid; we're
            # updating the version pointer so it matches the
            # submission's new pin. WITHOUT this, the
            # `list_submission_versions` join would return the OLD
            # form_version_id (where the Steps still point) but the
            # submission's snap reports the NEW one — the view
            # layer then sees its default `viewing_version_id`
            # missing from `available` and 404s.
            #
            # Event rows are intentionally NOT migrated. An event's
            # `form_version_id` records WHEN the event happened
            # (which version was current at that time); rewriting
            # it would misrepresent history. The audit event
            # appended below correctly lands on the new minor.
            session.execute(
                update(Step)
                .where(
                    Step.submission_handle == sub.handle,
                    Step.form_version_id == from_id,
                )
                .values(form_version_id=new_form_version_id)
            )
            # Append an audit event. The submission's history tab
            # surfaces this so users see why the version changed.
            session.add(Event(
                submission_handle=sub.handle,
                type="submission_auto_repinned",
                form_version_id=new_form_version_id,
                occurred_at=now,
                payload={
                    "from_form_version_id": from_id,
                    "to_form_version_id": new_form_version_id,
                    "reason": "minor_bump",
                    "form_id": form_id,
                },
            ))
            migrated += 1
        session.commit()
        return migrated


def get_form_version(version_id: int) -> Optional[dict[str, Any]]:
    """A form_version's stored data — `form_id`, `version`,
    `minor_version`, `source`, `compiled_graph`. Used to reconstruct
    old versions for execution and to surface version metadata on
    the API."""
    with Session(_engine) as session:
        fv = session.get(FormVersion, version_id)
        if fv is None:
            return None
        return {
            "id": fv.id,
            "form_id": fv.form_id,
            "version": fv.version,
            "minor_version": fv.minor_version,
            "source": fv.source,
            "compiled_graph": fv.compiled_graph,
        }


def get_form_version_by_number(
    form_id: str, version: int, minor_version: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """A form_version's stored data, looked up by human-facing
    `(form_id, version[, minor_version])` instead of internal DB id.
    Returns the same shape as `get_form_version`. Used to surface a
    pinned submission's source.

    When `minor_version` is omitted, returns the LATEST minor for the
    requested major — what most callers want when they only know the
    integer version. When supplied explicitly, returns that exact row
    (or None if it doesn't exist)."""
    with Session(_engine) as session:
        stmt = select(FormVersion).where(
            FormVersion.form_id == form_id,
            FormVersion.version == version,
        )
        if minor_version is not None:
            stmt = stmt.where(FormVersion.minor_version == minor_version)
        else:
            stmt = stmt.order_by(FormVersion.minor_version.desc())
        fv = session.execute(stmt).scalars().first()
        if fv is None:
            return None
        return {
            "id": fv.id,
            "form_id": fv.form_id,
            "version": fv.version,
            "minor_version": fv.minor_version,
            "source": fv.source,
            "compiled_graph": fv.compiled_graph,
        }


def load_submission_frozen_chain(
    submission_handle: str, form_version_id: int,
) -> dict[str, Any]:
    """Load one historical chain of a submission — steps + events
    tagged to `form_version_id`. Used by the submission detail page
    when the user picks a non-active version in the history picker.

    Returns the same shape as one of the submission's entries from
    `load_submissions`, but with steps/events scoped to the requested
    version (the active read defaults to the current version; this is
    the explicit-version variant).
    """
    with Session(_engine) as session:
        sub = session.get(Submission, submission_handle)
        if sub is None:
            return {"steps": [], "events": []}
        steps = [
            s for s in sub.steps if s.form_version_id == form_version_id
        ]
        # Events whose tag matches the requested version. Pre-migration
        # events with a NULL tag are not included here — they would
        # appear as "v1" history under the backfill rule, which is
        # served by querying the *original* version. For events
        # recorded before the form_version_id column existed at all
        # (untagged after backfill failed), they remain invisible to
        # the version picker — acceptable, since the picker exists
        # to show per-version history of a re-pinned submission.
        events = [
            e for e in sub.events
            if e.form_version_id == form_version_id
        ]
        return {
            "steps": [
                {
                    "seq": s.seq,
                    "node_id": s.node_id,
                    "page_id": s.page_id,
                    "kind": s.kind,
                    "state": s.state,
                    "started_at": _aware(s.started_at),
                    "submitted_at": _aware(s.submitted_at),
                    "form_values": s.form_values,
                    "backend_return": s.backend_return,
                    "external_state": s.external_state or {},
                    "button_clicked": s.button_clicked,
                    "next_node_id": s.next_node_id,
                    "branch_explicit": s.branch_explicit,
                }
                for s in steps
            ],
            "events": [
                {
                    "type": e.type,
                    "node_id": e.node_id,
                    "page_id": e.page_id,
                    "form_version_id": e.form_version_id,
                    "occurred_at": _aware(e.occurred_at),
                    "payload": e.payload,
                }
                for e in events
            ],
        }


def list_submission_versions(
    submission_handle: str,
) -> list[dict[str, Any]]:
    """Every form_version this submission has data on.

    Powers the version picker on the submission summary page. Returns
    `[{id, version, minor_version}, ...]` ordered by (version, minor)
    ascending. A submission that has never re-pinned has one entry;
    one that has been force-re-pinned has one per version it has
    lived under.
    """
    with Session(_engine) as session:
        rows = session.execute(
            select(
                FormVersion.id,
                FormVersion.version,
                FormVersion.minor_version,
            )
            .join(Step, Step.form_version_id == FormVersion.id)
            .where(Step.submission_handle == submission_handle)
            .distinct()
            .order_by(
                FormVersion.version.asc(),
                FormVersion.minor_version.asc(),
            )
        ).all()
        return [
            {"id": r[0], "version": r[1], "minor_version": r[2]}
            for r in rows
        ]


# --- Submission write-through ----------------------------------------------


def sync_submission(snapshot: dict[str, Any]) -> None:
    """Mirror a submission's current state to the database.

    `snapshot` is a plain dict assembled by the runtime (it owns the
    in-memory types). Shape:

        handle, submission_id, form_version_id, state, created_at,
        terminated_at, error,
        steps:  [{seq, node_id, page_id, kind, state, started_at,
                  submitted_at, form_values, button_clicked,
                  next_node_id, branch_explicit}, ...],
        events: [{type, node_id, page_id, occurred_at, payload}, ...]
                — the FULL event list; only those beyond what's already
                stored are appended (events are append-only).

    Idempotent: upserts the submission, replaces its step rows to match
    the current chain, and appends any new events.
    """
    with Session(_engine) as session:
        sub = session.get(Submission, snapshot["handle"])
        if sub is None:
            sub = Submission(handle=snapshot["handle"])
            session.add(sub)

        sub.submission_id = snapshot["submission_id"]
        sub.form_version_id = snapshot["form_version_id"]
        sub.state = snapshot["state"]
        # Defaults to a real submission, so a caller that predates
        # control panels — or a snapshot built without the field —
        # keeps counting as one.
        sub.kind = snapshot.get("kind") or "submission"
        sub.created_at = snapshot["created_at"]
        sub.terminated_at = snapshot["terminated_at"]
        sub.error = snapshot["error"]
        sub.cleared_run_ids = snapshot.get("cleared_run_ids") or None
        # Every sync reflects a state change — bump the update time so
        # the export API's `updated_since` re-sync sees it.
        sub.updated_at = _utcnow()

        # Steps mirror the current chain — drop and rewrite. Scope to
        # the *active version's* rows; frozen chains from prior versions
        # stay untouched (they are read-only history).
        active_vid = sub.form_version_id
        for existing in list(sub.steps):
            if existing.form_version_id == active_vid:
                session.delete(existing)
        # Keep the relationship loadable while we mutate it.
        session.flush()
        for s in snapshot["steps"]:
            sub.steps.append(
                Step(
                    submission_handle=sub.handle,
                    form_version_id=active_vid,
                    seq=s["seq"],
                    node_id=s["node_id"],
                    page_id=s["page_id"],
                    kind=s["kind"],
                    state=s["state"],
                    started_at=s["started_at"],
                    submitted_at=s["submitted_at"],
                    form_values=s["form_values"],
                    backend_return=s["backend_return"],
                    external_state=s.get("external_state") or None,
                    button_clicked=s["button_clicked"],
                    next_node_id=s["next_node_id"],
                    branch_explicit=s["branch_explicit"],
                    user_id=s.get("user_id"),
                    error=s.get("error"),
                    traceback=s.get("traceback"),
                )
            )

        # Events are append-only: keep what's stored, add the rest.
        already = session.scalar(
            select(func.count(Event.id)).where(
                Event.submission_handle == sub.handle
            )
        )
        for e in snapshot["events"][already:]:
            session.add(
                Event(
                    submission_handle=sub.handle,
                    type=e["type"],
                    node_id=e["node_id"],
                    page_id=e["page_id"],
                    form_version_id=e.get("form_version_id"),
                    occurred_at=e["occurred_at"],
                    payload=e["payload"],
                )
            )

        session.commit()


def list_all_submission_ids() -> dict[str, str]:
    """Lightweight read: every persisted (submission_id, handle) pair,
    used at startup to populate the runtime's `_id_index` BEFORE the
    full hydrate loop runs.

    Why a separate read: `load_submissions()` does the full snapshot
    + workflow-version recompile dance, which skips submissions whose
    form source no longer compiles (a DSL rename, a removed file).
    Those skipped submissions still hold submission_ids in the DB; if
    the in-memory index doesn't know about them, a new submission can
    mint a colliding id and fail at the DB UNIQUE constraint instead
    of at the clean in-memory check.

    Returns a dict mapping submission_id → handle. Rows without a
    submission_id (drafts that never minted one) are skipped — they
    can't collide. If two rows somehow share a submission_id (the DB
    constraint should prevent this; defensive), the last one wins
    silently — the index is for collision prevention, not as a source
    of truth."""
    with Session(_engine) as session:
        rows = session.execute(
            select(Submission.submission_id, Submission.handle).where(
                Submission.submission_id.is_not(None),
            )
        ).all()
        return {sid: handle for sid, handle in rows}


def delete_submissions_for_form(form_id: str) -> list[tuple[str, str | None]]:
    """Wipe every submission of a form — used by `frontflow example
    seed --reset` to make re-seeding idempotent.

    Submissions are matched by joining through their form_version row
    (form_id lives on FormVersion, not Submission). Steps and Events
    cascade via the ORM relationships; SubmissionBlob rows share the
    handle FK without a relationship, so they're deleted separately
    in the same transaction.

    Returns a list of (handle, submission_id) pairs for every wiped
    submission — the caller needs these to also evict matching
    entries from the runtime's in-memory `_id_index` and
    `_submissions` maps. The DB delete alone leaves those caches
    holding stale references; a fresh mint would collide on the
    in-memory check even though the DB row is gone. `submission_id`
    is None for drafts that never minted one.
    """
    with Session(_engine) as session:
        version_ids = session.scalars(
            select(FormVersion.id).where(FormVersion.form_id == form_id)
        ).all()
        if not version_ids:
            return []
        rows = session.execute(
            select(Submission.handle, Submission.submission_id).where(
                Submission.form_version_id.in_(version_ids),
            )
        ).all()
        if not rows:
            return []
        handles = [h for h, _ in rows]
        # Blobs first (no cascade relationship — manual delete).
        session.execute(
            delete(SubmissionBlob).where(
                SubmissionBlob.submission_handle.in_(handles),
            )
        )
        # Submissions — cascade clears steps + events via relationship.
        for sub in session.scalars(
            select(Submission).where(Submission.handle.in_(handles))
        ):
            session.delete(sub)
        session.commit()
        return [(h, sid) for h, sid in rows]


def soft_delete_submissions_by_handles(
    handles: list[str], form_id: str,
) -> tuple[list[tuple[str, str | None]], list[str]]:
    """Tombstone the named submissions by setting `deleted_at = now`.
    No rows are physically removed — Step / Event / SubmissionBlob
    history stays intact so an undelete (out of scope for v1) can
    lift the tombstone.

    Scoped to `form_id` defensively: a handle resolves only if it
    belongs to a submission of THIS form. This stops a caller with
    permissions on form A from soft-deleting form B's submissions
    by passing B's handles into A's endpoint. The endpoint already
    requires admin, so this is defense-in-depth; the cost is one
    extra join clause.

    Already-deleted rows are treated as "not found" and reported in
    that bucket — they were never deletable from the current UI's
    point of view (the listing skips them), so a re-issued request
    against a deleted handle is genuinely an unknown handle from
    the caller's frame of reference.

    Returns `(deleted, not_found)`:
      - `deleted`: list of (handle, submission_id) pairs that were
        actually tombstoned. The caller uses these to evict from
        in-memory `_submissions` / `_id_index`. `submission_id` is
        None for drafts that never minted one.
      - `not_found`: list of input handles that didn't resolve to a
        live submission for this form (either unknown, on a
        different form, or already deleted).
    """
    if not handles:
        return ([], [])
    with Session(_engine) as session:
        version_ids = session.scalars(
            select(FormVersion.id).where(FormVersion.form_id == form_id)
        ).all()
        if not version_ids:
            # Form has no versions → no submissions to scope to;
            # every input handle is "not found" from here.
            return ([], list(handles))

        # Resolve the requested handles to (handle, submission_id)
        # pairs for rows that are (1) on this form's versions, and
        # (2) not already tombstoned. Anything missing from this
        # result goes into `not_found`.
        live_rows = session.execute(
            select(Submission.handle, Submission.submission_id)
            .where(
                Submission.handle.in_(handles),
                Submission.form_version_id.in_(version_ids),
                Submission.deleted_at.is_(None),
            )
        ).all()
        live_handles = {h for h, _ in live_rows}
        not_found = [h for h in handles if h not in live_handles]

        if live_handles:
            now = _utcnow()
            session.execute(
                update(Submission)
                .where(Submission.handle.in_(live_handles))
                .values(deleted_at=now, updated_at=now)
            )
            session.commit()
        return (
            [(h, sid) for h, sid in live_rows],
            not_found,
        )


def load_submissions() -> list[dict[str, Any]]:
    """Read every persisted submission back out, as plain dicts the
    runtime rehydrates into its in-memory types. Same shape as the
    `sync_submission` snapshot. Soft-deleted submissions (those
    with a non-NULL `deleted_at`) are skipped — they should not
    re-enter the in-memory working set on a restart."""
    out: list[dict[str, Any]] = []
    with Session(_engine) as session:
        for sub in session.scalars(
            select(Submission).where(Submission.deleted_at.is_(None))
        ):
            out.append(
                {
                    "handle": sub.handle,
                    "submission_id": sub.submission_id,
                    "form_version_id": sub.form_version_id,
                    "state": sub.state,
                    # Carried through rehydration, or a control panel
                    # would come back from a restart as a submission.
                    "kind": sub.kind or "submission",
                    "created_at": _aware(sub.created_at),
                    "terminated_at": _aware(sub.terminated_at),
                    "error": sub.error,
                    "cleared_run_ids": sub.cleared_run_ids or {},
                    "steps": [
                        {
                            "seq": s.seq,
                            "node_id": s.node_id,
                            "page_id": s.page_id,
                            "kind": s.kind,
                            "state": s.state,
                            "started_at": _aware(s.started_at),
                            "submitted_at": _aware(s.submitted_at),
                            "form_values": s.form_values,
                            "backend_return": s.backend_return,
                            "external_state": s.external_state or {},
                            "button_clicked": s.button_clicked,
                            "next_node_id": s.next_node_id,
                            "branch_explicit": s.branch_explicit,
                            "user_id": s.user_id,
                            "error": s.error,
                            "traceback": s.traceback,
                        }
                        for s in sub.steps
                        if s.form_version_id == sub.form_version_id
                    ],
                    "events": [
                        {
                            "type": e.type,
                            "node_id": e.node_id,
                            "page_id": e.page_id,
                            "form_version_id": e.form_version_id,
                            "occurred_at": _aware(e.occurred_at),
                            "payload": e.payload,
                        }
                        for e in sub.events
                    ],
                }
            )
    return out


# --- Admin: read-only listing + tracking -----------------------------------


def form_exists(form_id: str) -> bool:
    """Whether a form is known to the data backend — true even for an
    archived (not-live) form that still holds historical submissions."""
    with Session(_engine) as session:
        return session.get(Form, form_id) is not None


def _is_submission():
    """A predicate matching real submissions, excluding control-panel
    sessions.

    One helper rather than the condition written out at each call site,
    because the failure mode is asymmetric: forget it somewhere and
    sessions leak back into a count or a report, and nothing about the
    result looks wrong until someone asks why a form has submissions
    nobody made.

    `kind IS NULL` counts as a submission — rows written before the
    column existed are all real.
    """
    return or_(Submission.kind.is_(None), Submission.kind == "submission")


def list_forms_overview() -> list[dict[str, Any]]:
    """Every form with its version count, submission counts by state,
    and last-activity timestamp — the data behind the admin forms list.
    Ordered by folder then form id."""
    with Session(_engine) as session:
        forms = session.scalars(
            select(Form).order_by(Form.folder_path, Form.form_id)
        ).all()

        version_counts = dict(
            session.execute(
                select(FormVersion.form_id, func.count(FormVersion.id))
                .group_by(FormVersion.form_id)
            ).all()
        )

        # Submission counts grouped by (form, state). Excludes
        # soft-deleted rows so the stat tiles match what the
        # submissions tab shows, and control-panel sessions, which are
        # working surfaces rather than submissions — they never
        # terminate, so counting them would park a permanent `running`
        # in every total.
        counts: dict[str, dict[str, int]] = {}
        for form_id, state, n in session.execute(
            select(
                FormVersion.form_id,
                Submission.state,
                func.count(Submission.handle),
            )
            .join(Submission, Submission.form_version_id == FormVersion.id)
            .where(
                Submission.deleted_at.is_(None),
                _is_submission(),
            )
            .group_by(FormVersion.form_id, Submission.state)
        ).all():
            counts.setdefault(form_id, {})[state] = n

        # Last activity = the most recent event of any of the form's
        # submissions (truthful even for still-running submissions).
        # We also keep the submission's id so the form-summary KPI can
        # link directly to the submission that produced the timestamp.
        # Soft-deleted submissions are excluded from this signal —
        # otherwise a deleted (and presumably forgotten) row could
        # remain the form's "latest activity" forever.
        from sqlalchemy import desc as _desc, func as _func
        # Use ROW_NUMBER over form_id partitioned by occurred_at to
        # pick the single latest event row per form, then read the
        # timestamp, the originating submission's public id, and that
        # submission's current state — the state lets the form-summary
        # KPI color the "Last activity" timestamp by what kind of
        # activity it was (success/failed/running).
        ranked = (
            select(
                FormVersion.form_id.label("form_id"),
                Event.occurred_at.label("occurred_at"),
                Submission.submission_id.label("submission_id"),
                Submission.state.label("submission_state"),
                _func.row_number()
                .over(
                    partition_by=FormVersion.form_id,
                    order_by=_desc(Event.occurred_at),
                )
                .label("rn"),
            )
            .join(Submission, Submission.form_version_id == FormVersion.id)
            .join(Event, Event.submission_handle == Submission.handle)
            .where(Submission.deleted_at.is_(None), _is_submission())
            .subquery()
        )
        last_activity: dict[str, datetime] = {}
        last_activity_submission_id: dict[str, Optional[str]] = {}
        last_activity_state: dict[str, Optional[str]] = {}
        for form_id, ts, sid, st in session.execute(
            select(
                ranked.c.form_id,
                ranked.c.occurred_at,
                ranked.c.submission_id,
                ranked.c.submission_state,
            ).where(ranked.c.rn == 1)
        ).all():
            last_activity[form_id] = _aware(ts)
            last_activity_submission_id[form_id] = sid
            last_activity_state[form_id] = st

        overview: list[dict[str, Any]] = []
        for f in forms:
            c = counts.get(f.form_id, {})
            running = c.get("running", 0)
            success = c.get("success", 0)
            failed = c.get("failed", 0)
            overview.append(
                {
                    "form_id": f.form_id,
                    "name": f.name,
                    "folder_path": f.folder_path,
                    "is_live": f.is_live,
                    "version_count": version_counts.get(f.form_id, 0),
                    "submissions": {
                        "running": running,
                        "success": success,
                        "failed": failed,
                        "total": running + success + failed,
                    },
                    "last_activity": last_activity.get(f.form_id),
                    "last_activity_submission_id": last_activity_submission_id.get(
                        f.form_id,
                    ),
                    "last_activity_state": last_activity_state.get(f.form_id),
                }
            )
        return overview


def list_all_form_submissions(form_id: str) -> list[dict[str, Any]]:
    """Every submission of a form, newest first — as a flat list,
    not a page envelope. For the analytics endpoints (state-mix,
    step-counts, time-per-step, etc.) which need to enumerate the
    full set rather than paginate. Soft-deleted rows are excluded.

    Use `list_form_submissions` instead for the user-facing
    listing tab — that one paginates, filters, and sorts.
    """
    with Session(_engine) as session:
        rows = session.execute(
            select(Submission, FormVersion.version)
            .join(FormVersion, Submission.form_version_id == FormVersion.id)
            .where(
                FormVersion.form_id == form_id,
                Submission.deleted_at.is_(None),
                _is_submission(),
            )
            .order_by(Submission.created_at.desc())
            .options(selectinload(Submission.steps))
        ).all()
        return [
            {
                "submission_id": sub.submission_id,
                "handle": sub.handle,
                "state": sub.state,
                "form_version": version,
                "created_at": _aware(sub.created_at),
                "updated_at": _aware(sub.updated_at),
                "terminated_at": _aware(sub.terminated_at),
                "current_step": (
                    sub.steps[-1].node_id if sub.steps else None
                ),
            }
            for sub, version in rows
        ]


#: Whitelist of sortable columns on the submissions listing.
#: Maps the public sort key (URL- and API-facing) to the SQLAlchemy
#: column the ORDER BY actually uses. Keeping a static map is the
#: trust boundary: an unknown key from the request becomes a 400 in
#: the endpoint layer rather than reaching the ORM as a string.
#:
#: `current_step` is intentionally absent — it's a derived value (the
#: last Step row's node_id) and sorting on it would require a
#: correlated subquery per row. We can add it later as a window-
#: function ORDER BY but the simple sort cases land first.
_LISTING_SORT_COLUMNS: dict[str, Any] = {}
# Populated below at module import time after the models exist.


def list_form_submissions(
    form_id: str,
    *,
    limit: int = 25,
    offset: int = 0,
    states: Optional[Sequence[str]] = None,
    query: Optional[str] = None,
    sort: Optional[Sequence[tuple[str, str]]] = None,
    handle_whitelist: Optional[set[str]] = None,
    include_deleted: bool = False,
    updated_since: Optional[datetime] = None,
    updated_before: Optional[datetime] = None,
    current_steps: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """The form's submissions, server-paginated.

    Soft-deleted rows are excluded unless `include_deleted=True`. The
    listing endpoint gates that flag behind an admin check before
    passing it down — non-admins can never see tombstoned rows.

    # TODO(unify-listing-and-export): see ROADMAP "Unify submission-
    # listing and submission-export endpoints". Both this function
    # and `export_submissions` filter by `updated_since` /
    # `updated_before` over `Submission.updated_at` and paginate the
    # same data — the unified endpoint folds them together.

    Parameters
    ----------
    limit, offset
        Page window. `limit` is the page size; `offset` is the number
        of rows to skip before this page. The endpoint caps `limit`
        at a sane maximum (see `_LISTING_MAX_LIMIT`) — past the cap
        the request degrades to that value rather than 400-ing, so a
        runaway client doesn't break the listing entirely.
    states
        Multi-select filter on `Submission.state`. None or empty list
        means "no state filter". Unknown state values are silently
        ignored (a typo'd URL param shouldn't 500 the listing).
    query
        Case-insensitive substring match on `submission_id` OR
        `handle`. Empty or None means "no search". We don't search
        derived fields (current_step, version) — adding them silently
        broadens the match in ways users wouldn't expect.
    sort
        Ordered list of (column_key, direction) pairs. Direction is
        `"asc"` or `"desc"`. Column keys are validated against
        `_LISTING_SORT_COLUMNS`; unknowns are dropped. The first
        entry is the primary sort, subsequent entries break ties.
        A final tiebreaker on `Submission.handle` is always appended
        so pagination is deterministic — without it, two rows
        identical on every named sort column would shuffle between
        page boundaries on refetch. Default sort (when sort is None
        or empty after validation) is `(created_at, desc)`.
    handle_whitelist
        When set, restrict the result to submissions whose handle is
        in this set. The endpoint passes the output of the bulk
        visibility helper here, so a non-admin user with limited
        view rights paginates over THEIR visible rows correctly
        instead of getting partially-filled pages from a
        paginate-then-filter pipeline. None means "no whitelist"
        (admin / folder-grant case).
    include_deleted
        When True, soft-deleted rows are included in the result.
        Their `deleted_at` is non-null in the response row, which
        the UI uses to render a "deleted" pill and disable the
        click-through (the in-memory submission was evicted on
        soft-delete so the detail page would 404). Defaults False
        so the tab's default view stays tombstone-free.
    updated_since, updated_before
        Half-open interval `[updated_since, updated_before)` on
        `Submission.updated_at` (the "last activity" axis). Either
        bound may be set independently. The UI passes calendar-date
        bounds (start-of-day for `since`, start-of-next-day for
        `before`) so a single-date filter still captures everything
        stamped during that day. Rows with a NULL `updated_at` (rare;
        only legacy rows from before the column existed) are
        excluded whenever either bound is set — otherwise the
        window's lower-bound check would silently include them.
    current_steps
        Multi-select filter on the submission's *current* step (the
        last step row's node_id under the relationship's
        `(form_version_id, seq)` ordering). Implemented via a
        window-numbered subquery — `row_number()` ranks each
        submission's steps and we keep submissions whose top-ranked
        step matches. Unknowns are dropped silently (a missing node
        in the form's source after an edit shouldn't break stale
        URLs).

    Returns
    -------
    ``{submissions, total, limit, offset}``

    ``total`` is the row count BEFORE the limit/offset is applied —
    so the UI can show "Showing 51–75 of 1,247" and gate the "next
    page" button. It DOES include the same filter set as the page
    query (state + q + window + step + whitelist + delete-flag); a
    filtered total is the only useful number.
    """
    # Clamp limit on the store side too, defense-in-depth. The
    # endpoint clamps first; the store re-clamps in case a future
    # internal caller forgets.
    limit = max(1, min(int(limit), _LISTING_MAX_LIMIT))
    offset = max(0, int(offset))

    # An empty whitelist set is "user can see nothing" — short-circuit
    # to an empty page rather than running a query whose IN-clause
    # is always false.
    if handle_whitelist is not None and not handle_whitelist:
        return {
            "submissions": [], "total": 0,
            "limit": limit, "offset": offset,
        }

    with Session(_engine) as session:
        # Precompute the current-step matching subquery once; reuse
        # in both the count and page statements. None when no
        # current_step filter is requested — keeps the join out of
        # the SQL when not needed.
        matching_handles_subq = None
        if current_steps:
            # Window-rank steps per submission by (form_version_id,
            # seq) descending — the same order the runtime uses to
            # pick "the last step" (Submission.steps relationship
            # has `order_by="(Step.form_version_id, Step.seq)"`).
            last_step_rank = (
                select(
                    Step.submission_handle.label("handle"),
                    Step.node_id.label("node_id"),
                    func.row_number()
                    .over(
                        partition_by=Step.submission_handle,
                        order_by=(
                            Step.form_version_id.desc(),
                            Step.seq.desc(),
                        ),
                    )
                    .label("rn"),
                )
                .subquery()
            )
            matching_handles_subq = (
                select(last_step_rank.c.handle)
                .where(
                    last_step_rank.c.rn == 1,
                    last_step_rank.c.node_id.in_(current_steps),
                )
                .scalar_subquery()
            )

        # Base WHERE — applies to both the page query and the total
        # count. Build once, reuse via the local helper.
        def _apply_filters(stmt):
            stmt = stmt.where(FormVersion.form_id == form_id, _is_submission())
            if not include_deleted:
                stmt = stmt.where(Submission.deleted_at.is_(None))
            if handle_whitelist is not None:
                stmt = stmt.where(
                    Submission.handle.in_(handle_whitelist)
                )
            if states:
                allowed = [
                    s for s in states
                    if s in _SUBMISSION_KNOWN_STATES
                ]
                if allowed:
                    stmt = stmt.where(Submission.state.in_(allowed))
            if query:
                escaped = (
                    query.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                pattern = f"%{escaped.lower()}%"
                stmt = stmt.where(
                    func.lower(
                        func.coalesce(Submission.submission_id, "")
                    ).like(pattern, escape="\\")
                    | func.lower(Submission.handle).like(
                        pattern, escape="\\"
                    )
                )
            if updated_since is not None:
                stmt = stmt.where(
                    Submission.updated_at >= updated_since,
                    Submission.updated_at.is_not(None),
                )
            if updated_before is not None:
                stmt = stmt.where(
                    Submission.updated_at < updated_before,
                    Submission.updated_at.is_not(None),
                )
            if matching_handles_subq is not None:
                stmt = stmt.where(
                    Submission.handle.in_(matching_handles_subq)
                )
            return stmt

        # Total — same filters, no order/limit. SQLAlchemy 2.x style:
        # use `select(func.count())` on a subquery of the filtered
        # ids so the count is correct even when the page query joins
        # additional tables for sorting later.
        total = session.scalar(
            _apply_filters(
                select(func.count(Submission.handle)).join(
                    FormVersion,
                    Submission.form_version_id == FormVersion.id,
                )
            )
        ) or 0

        # Page query — applies the order spec on top of the filters.
        page_stmt = _apply_filters(
            select(Submission, FormVersion.version)
            .join(FormVersion, Submission.form_version_id == FormVersion.id)
            .options(selectinload(Submission.steps))
        )

        # Resolve sort spec against the whitelist. Unknown keys are
        # dropped; an empty spec falls back to the default.
        resolved_sort: list[tuple[Any, str]] = []
        for key, direction in (sort or ()):
            col = _LISTING_SORT_COLUMNS.get(key)
            if col is None:
                continue
            if direction not in ("asc", "desc"):
                continue
            resolved_sort.append((col, direction))
        if not resolved_sort:
            resolved_sort = [(Submission.created_at, "desc")]

        # Apply ORDER BY. `nulls_last()` regardless of direction —
        # NULL is "not yet" (an in-flight row missing updated_at,
        # a draft missing submission_id, etc.). Putting NULL at the
        # end matches the user's mental model: blank → least-known,
        # not "smallest" or "largest".
        order_clauses: list[Any] = []
        for col, direction in resolved_sort:
            clause = col.desc() if direction == "desc" else col.asc()
            order_clauses.append(clause.nulls_last())
        # Final tiebreaker on handle — deterministic pagination
        # across refetches, even when two rows are identical on
        # every user-named sort column.
        order_clauses.append(Submission.handle.asc())
        page_stmt = page_stmt.order_by(*order_clauses)

        rows = session.execute(
            page_stmt.limit(limit).offset(offset)
        ).all()

        return {
            "submissions": [
                {
                    "submission_id": sub.submission_id,
                    "handle": sub.handle,
                    "state": sub.state,
                    "form_version": version,
                    "created_at": _aware(sub.created_at),
                    "updated_at": _aware(sub.updated_at),
                    "terminated_at": _aware(sub.terminated_at),
                    "deleted_at": _aware(sub.deleted_at),
                    "current_step": (
                        sub.steps[-1].node_id if sub.steps else None
                    ),
                }
                for sub, version in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }


# Listing config that needs the model classes to exist at import time.
_LISTING_MAX_LIMIT = 100
_SUBMISSION_KNOWN_STATES = {"running", "success", "failed"}
_LISTING_SORT_COLUMNS.update({
    "submission_id": Submission.submission_id,
    "state": Submission.state,
    "form_version": FormVersion.version,
    "created_at": Submission.created_at,
    "updated_at": Submission.updated_at,
})


def list_form_submission_current_steps(
    form_id: str,
    *,
    include_deleted: bool = False,
) -> list[dict[str, Any]]:
    """Distinct `current_step` values across this form's submissions,
    with the count of submissions currently AT each step. Backs the
    "current step" filter dropdown on the submissions tab.

    "Current" = the last step row per submission under the same
    `(form_version_id, seq)` ordering the runtime uses to compute
    each submission's `current_step`. Window-ranked subquery picks
    the top row per submission; then we group by node_id.

    Ordered by count desc — most-common steps surface first in the
    dropdown, which is usually what the user wants to filter on.
    Deleted submissions are excluded unless `include_deleted=True`;
    the endpoint gates the flag behind admin.

    Returns `[{"node_id": str, "count": int}]`. An empty list means
    the form has no submissions yet — the dropdown renders an
    empty/disabled state.
    """
    with Session(_engine) as session:
        # Rank steps per submission by (form_version_id, seq) desc;
        # row_number=1 is the submission's current step.
        last_step_rank = (
            select(
                Step.submission_handle.label("handle"),
                Step.node_id.label("node_id"),
                func.row_number()
                .over(
                    partition_by=Step.submission_handle,
                    order_by=(
                        Step.form_version_id.desc(),
                        Step.seq.desc(),
                    ),
                )
                .label("rn"),
            )
            .subquery()
        )
        stmt = (
            select(
                last_step_rank.c.node_id,
                func.count(Submission.handle).label("cnt"),
            )
            .join(
                Submission,
                Submission.handle == last_step_rank.c.handle,
            )
            .join(
                FormVersion,
                Submission.form_version_id == FormVersion.id,
            )
            .where(
                last_step_rank.c.rn == 1,
                FormVersion.form_id == form_id,
            )
            .group_by(last_step_rank.c.node_id)
            .order_by(func.count(Submission.handle).desc())
        )
        stmt = stmt.where(_is_submission())
        if not include_deleted:
            stmt = stmt.where(Submission.deleted_at.is_(None))
        rows = session.execute(stmt).all()
        return [
            {"node_id": node_id, "count": int(cnt)}
            for node_id, cnt in rows
        ]


def list_submission_step_history(
    form_id: str, submission_id: str
) -> list[str]:
    """The ordered sequence of node ids this submission has visited,
    one entry per `Step` row in registration order. Used by the
    analytics step-counts endpoint to ask "how many submissions
    reached each step" (including ones that moved past).

    Re-entries via edit cascade appear as repeated node ids — the
    caller is expected to dedupe per its semantics. The accepted
    submission identifier is either a minted `submission_id` or a
    `handle` (for drafts that haven't minted yet)."""
    with Session(_engine) as session:
        sub = session.execute(
            select(Submission).where(
                (Submission.submission_id == submission_id)
                | (Submission.handle == submission_id)
            ).options(selectinload(Submission.steps))
        ).scalar_one_or_none()
        if sub is None:
            return []
        # The relation is loaded in step-insertion order (FormVersion
        # filtering, if any, happens at higher layers; for analytics
        # we want every step the submission has ever had a row for).
        return [s.node_id for s in sub.steps]


def list_submission_step_timing(
    form_id: str, submission_id: str
) -> list[dict[str, Any]]:
    """Per-step timing for one submission. Returns a list of dicts
    with `node_id`, `started_at`, and `submitted_at` in step order.
    Used by the analytics time-per-step endpoint to compute time-
    in-step deltas (consecutive started_at differences).

    A step that's currently awaiting has `submitted_at = None`;
    the caller decides whether to drop, count as in-progress, or
    treat as right-censored."""
    with Session(_engine) as session:
        sub = session.execute(
            select(Submission).where(
                (Submission.submission_id == submission_id)
                | (Submission.handle == submission_id)
            ).options(selectinload(Submission.steps))
        ).scalar_one_or_none()
        if sub is None:
            return []
        return [
            {
                "node_id": s.node_id,
                "started_at": _aware(s.started_at) if s.started_at else None,
                "submitted_at": _aware(s.submitted_at) if s.submitted_at else None,
            }
            for s in sub.steps
        ]


# --- Submission export -----------------------------------------------------
#
# A pull-based batch export: a consumer walks every submission (any
# state, in-flight included) in stable keyset order, or asks for just
# what changed since a timestamp. The cursor is an opaque encoding of
# the last (created_at, handle) seen — keyset pagination, so a consumer
# can resume exactly and new rows never shift a page.


def _encode_cursor(axis: str, ts: datetime, handle: str) -> str:
    """Opaque pagination cursor from a row's keyset position.

    `axis` records which timestamp the walk is ordered by — "created"
    or "updated" — so a cursor can only be replayed against the same
    ordering it was issued for.
    """
    raw = json.dumps(
        {"a": axis, "t": ts.isoformat(), "h": handle},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[str, datetime, str]:
    """Inverse of _encode_cursor → (axis, timestamp, handle). Raises
    ValueError on a malformed cursor."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        obj = json.loads(raw)
        axis = obj["a"]
        if axis not in ("created", "updated"):
            raise ValueError(f"unknown cursor axis: {axis!r}")
        return axis, datetime.fromisoformat(obj["t"]), obj["h"]
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"invalid cursor: {cursor!r}") from e


def export_submissions(
    *,
    limit: int = 100,
    cursor: Optional[str] = None,
    updated_since: Optional[datetime] = None,
    updated_before: Optional[datetime] = None,
    form_id: Optional[str] = None,
    terminal_only: bool = False,
    versions: str = "active",
) -> dict[str, Any]:
    """A page of submission records for the batch export API.

    Submissions of every state are included by default — in-flight as
    well as terminated — so a consumer sees the live picture.

      limit           page size (the caller clamps the range)
      cursor          resume after this opaque position; None starts over
      updated_since   only submissions updated at/after this time (>=)
      updated_before  only submissions updated strictly before this (<)
      form_id         restrict to one form
      terminal_only   only terminated submissions (terminated_at set)

    `updated_since` and `updated_before` together form a half-open
    interval [since, before) on the submission's last-update time —
    the idempotent data-interval query a batch job wants: the same
    window always yields the same rows.

    Note on lifecycle semantics: a submission's update time moves on
    every state change, so an in-flight submission can fall into
    several successive windows (once per change). The feed is
    therefore at-least-once — a consumer upserts on `submission_id`.
    `terminal_only` narrows to terminated submissions, whose update
    time no longer moves; each then lands in a single window, unless
    an operator explicitly retries it (a retry re-runs the submission
    and is genuinely new data).

    Ordering and the keyset cursor follow the *update* time whenever an
    interval bound is given (so paginating within a window stays on the
    axis the window filters on), and the *creation* time otherwise.

    Returns {"submissions": [...], "next_cursor": str | None}. A null
    next_cursor means the page set is exhausted.
    """
    if versions not in ("active", "all"):
        raise ValueError("versions must be 'active' or 'all'")
    # An interval query walks the update axis; a plain walk uses the
    # creation axis. Keeping the cursor on the same axis it filters on
    # is what makes a windowed pull both correct and idempotent.
    by_update = updated_since is not None or updated_before is not None
    axis = "updated" if by_update else "created"
    sort_col = (
        Submission.updated_at if by_update else Submission.created_at
    )

    with Session(_engine) as session:
        stmt = (
            select(Submission, FormVersion.version, FormVersion.form_id)
            .join(
                FormVersion,
                Submission.form_version_id == FormVersion.id,
            )
            .options(selectinload(Submission.steps))
            .order_by(sort_col.asc(), Submission.handle.asc())
        )

        if form_id is not None:
            stmt = stmt.where(FormVersion.form_id == form_id)

        # Soft-deleted submissions never appear in the listing. The
        # tab UI calls this; non-NULL deleted_at means an admin has
        # tombstoned the row — it shouldn't surface in pagination
        # any more than a hard-deleted row would have.
        stmt = stmt.where(Submission.deleted_at.is_(None), _is_submission())

        if terminal_only:
            # A terminated submission's update time is frozen — this is
            # what makes a windowed pull lifecycle-idempotent.
            stmt = stmt.where(Submission.terminated_at.is_not(None))

        if updated_since is not None:
            stmt = stmt.where(Submission.updated_at >= updated_since)
        if updated_before is not None:
            stmt = stmt.where(Submission.updated_at < updated_before)

        if cursor is not None:
            c_axis, c_ts, c_handle = _decode_cursor(cursor)
            if c_axis != axis:
                # A cursor issued for a different ordering — replaying
                # it would silently skip or repeat rows.
                raise ValueError(
                    "cursor does not match this query — a cursor from "
                    "an interval query cannot be reused on a plain "
                    "walk, or vice versa"
                )
            # Keyset: rows strictly after the (timestamp, handle) seen.
            stmt = stmt.where(
                (sort_col > c_ts)
                | ((sort_col == c_ts) & (Submission.handle > c_handle))
            )

        # Fetch one extra row to detect whether more pages remain.
        rows = session.execute(stmt.limit(limit + 1)).all()
        has_more = len(rows) > limit
        page = rows[:limit]

        submissions = [
            _export_record(sub, version, fid, versions=versions)
            for sub, version, fid in page
        ]
        next_cursor = None
        if has_more and page:
            last_sub = page[-1][0]
            last_ts = (
                last_sub.updated_at
                if by_update
                else last_sub.created_at
            )
            next_cursor = _encode_cursor(axis, last_ts, last_sub.handle)
        return {"submissions": submissions, "next_cursor": next_cursor}


def _export_record(
    sub: "Submission", version: int, form_id: str,
    *, versions: str = "active",
) -> dict[str, Any]:
    """One submission as an export record — identity, lifecycle, and a
    compact per-step breakdown carrying the submitted form values.

    `versions='active'` (default) emits only the steps from the
    submission's current version. `versions='all'` includes every
    version's steps; each step carries `form_version_id` so consumers
    can partition by version."""
    return {
        "submission_id": sub.submission_id,
        "form_id": form_id,
        "form_version": version,
        "state": sub.state,
        "created_at": _iso(sub.created_at),
        "updated_at": _iso(sub.updated_at),
        "terminated_at": _iso(sub.terminated_at),
        "error": sub.error,
        "steps": [
            {
                "seq": s.seq,
                "form_version_id": s.form_version_id,
                "node_id": s.node_id,
                "page_id": s.page_id,
                "kind": s.kind,
                "state": s.state,
                "started_at": _iso(s.started_at),
                "submitted_at": _iso(s.submitted_at),
                "form_values": s.form_values,
                "button_clicked": s.button_clicked,
            }
            for s in sub.steps
            if versions == "all"
            or s.form_version_id == sub.form_version_id
        ],
    }


def _iso(dt: Optional[datetime]) -> Optional[str]:
    """A timezone-aware ISO 8601 string, or None."""
    aware = _aware(dt)
    return aware.isoformat() if aware is not None else None


def reclassify_sessions(*, dry_run: bool = True) -> list[dict[str, Any]]:
    """Recompute `kind` for running submissions from where they rest.

    The `kind` column was added with a blanket backfill to
    ``'submission'``, because a migration has no way to ask the DSL
    whether a node closes. Rows written before the column existed are
    therefore all marked as submissions, including control-panel
    sessions — so a filter panel someone opened thirty times reads as
    thirty submissions stuck at `running`, and inflates every count on
    the index.

    This re-derives the answer for rows the migration had to guess at,
    using the same rule the runtime applies at submit time: the button
    decides when it says so, otherwise the node does
    (`runtime._submit_closes_node`). Duplicating that rule here rather
    than importing it keeps this readable against a *stored* graph,
    where nodes and buttons are plain deserialized objects; the two must
    stay in step, and the test asserts they agree.

    Only `running` submissions are considered. A submission that reached
    a terminal state closed by definition, whatever its resting node now
    says.

    Idempotent: it compares before it writes, so a second run is a
    no-op. Returns what changed (or would change, under `dry_run`) as
    ``[{"handle", "form_id", "node_id", "from", "to"}]``.
    """
    from .compile import compiled_graph_to_workflow

    changes: list[dict[str, Any]] = []

    with Session(_engine) as session:
        # The submission's current step, ranked exactly as
        # list_form_submission_current_steps does it.
        last_step = (
            select(
                Step.submission_handle.label("handle"),
                Step.node_id.label("node_id"),
                Step.button_clicked.label("button_clicked"),
                func.row_number()
                .over(
                    partition_by=Step.submission_handle,
                    order_by=(Step.form_version_id.desc(), Step.seq.desc()),
                )
                .label("rn"),
            )
            .subquery()
        )
        rows = session.execute(
            select(
                Submission.handle,
                Submission.kind,
                FormVersion.form_id,
                FormVersion.compiled_graph,
                last_step.c.node_id,
                last_step.c.button_clicked,
            )
            .join(FormVersion, FormVersion.id == Submission.form_version_id)
            .join(last_step, last_step.c.handle == Submission.handle)
            .where(
                last_step.c.rn == 1,
                Submission.state == "running",
                Submission.deleted_at.is_(None),
            )
        ).all()

        # Deserializing a graph is not free, and many submissions share
        # a form version.
        graph_cache: dict[int, Any] = {}

        for handle, kind, form_id, graph, node_id, button_clicked in rows:
            key = id(graph)
            workflow = graph_cache.get(key)
            if workflow is None:
                try:
                    workflow = compiled_graph_to_workflow(graph)
                except Exception:  # noqa: BLE001
                    # A graph too old or too broken to deserialize is
                    # left exactly as it is. Guessing here is what
                    # created the problem in the first place.
                    continue
                graph_cache[key] = workflow

            node = next(
                (n for n in getattr(workflow, "nodes", []) if n.id == node_id), None
            )
            if node is None:
                continue

            closes = getattr(node, "closes", True)
            for button in getattr(node, "buttons", []):
                if button.id == button_clicked and button.advances is not None:
                    closes = button.advances
                    break

            derived = "submission" if closes else "session"
            if derived == (kind or "submission"):
                continue

            changes.append(
                {
                    "handle": handle,
                    "form_id": form_id,
                    "node_id": node_id,
                    "from": kind or "submission",
                    "to": derived,
                }
            )
            if not dry_run:
                session.execute(
                    update(Submission)
                    .where(Submission.handle == handle)
                    .values(kind=derived)
                )

        if not dry_run:
            session.commit()

    return changes
