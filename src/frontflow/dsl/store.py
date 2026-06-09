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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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
    select,
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
        back_populates="form", order_by="FormVersion.version"
    )


class FormVersion(Base):
    """A snapshot of one compiled state of a form. A new row is written
    only when the compiled structure changes (by content hash)."""

    __tablename__ = "form_version"
    __table_args__ = (UniqueConstraint("form_id", "content_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    form_id: Mapped[str] = mapped_column(
        ForeignKey("form.form_id"), nullable=False, index=True
    )
    # Monotonic per form — the human-facing "version 3".
    version: Mapped[int] = mapped_column(Integer, nullable=False)
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
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # Bumped on every state change — a new step, a clear, termination.
    # Drives the export API's `updated_since` incremental re-sync.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    terminated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
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
    _migrate_add_columns()


def _migrate_add_columns() -> None:
    """Lightweight migration — `create_all` adds missing tables but not
    missing columns on existing ones. Add columns introduced after a DB
    was first created. Uses SQLAlchemy's inspector so it works on both
    SQLite and Postgres."""
    inspector = inspect(_engine)

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
    if "updated_at" not in sub_cols:
        with _engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE submission ADD COLUMN updated_at DATETIME"
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


def upsert_form_version(
    form_id: str,
    name: str,
    folder_path: str,
    compiled_graph: dict[str, Any],
    content_hash: str,
    source: str,
    dsl_visibility: Optional[str] = None,
) -> int:
    """Record a form and its current compiled state. Inserts a new
    form_version only when the content hash differs from the latest.
    Returns the id of the form_version that is now current.

    `dsl_visibility` is the visibility the form's DSL declares (via
    `@form(private=True)` or similar). When set, it's enforced on
    EVERY scan — the DSL is the source of truth; the admin UI does
    not override settings declared in code. When `None`, the form's
    DSL is silent on visibility and the admin owns the value.

    Behavior:
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

        latest = session.scalars(
            select(FormVersion)
            .where(FormVersion.form_id == form_id)
            .order_by(FormVersion.version.desc())
        ).first()

        if latest is not None and latest.content_hash == content_hash:
            session.commit()
            return latest.id

        version = FormVersion(
            form_id=form_id,
            version=(latest.version + 1) if latest is not None else 1,
            content_hash=content_hash,
            compiled_graph=compiled_graph,
            source=source,
            created_at=now,
        )
        session.add(version)
        session.commit()
        return version.id


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


def get_form_version(version_id: int) -> Optional[dict[str, Any]]:
    """A form_version's stored data — `form_id`, `version`, `source`,
    `compiled_graph`. Used to reconstruct old versions for execution."""
    with Session(_engine) as session:
        fv = session.get(FormVersion, version_id)
        if fv is None:
            return None
        return {
            "id": fv.id,
            "form_id": fv.form_id,
            "version": fv.version,
            "source": fv.source,
            "compiled_graph": fv.compiled_graph,
        }


def get_form_version_by_number(
    form_id: str, version: int,
) -> Optional[dict[str, Any]]:
    """A form_version's stored data, looked up by the human-facing
    `(form_id, version)` pair instead of the internal DB id. Returns
    the same shape as `get_form_version`. Used to surface a pinned
    submission's source — the submission stores the integer version,
    not the DB id we use to look it up directly."""
    with Session(_engine) as session:
        stmt = select(FormVersion).where(
            FormVersion.form_id == form_id,
            FormVersion.version == version,
        )
        fv = session.execute(stmt).scalar_one_or_none()
        if fv is None:
            return None
        return {
            "id": fv.id,
            "form_id": fv.form_id,
            "version": fv.version,
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
    `[{id, version}, ...]` ordered by version number ascending. A
    submission that has never re-pinned has one entry; one that has
    been force-re-pinned has one per version it has lived under.
    """
    with Session(_engine) as session:
        rows = session.execute(
            select(FormVersion.id, FormVersion.version)
            .join(Step, Step.form_version_id == FormVersion.id)
            .where(Step.submission_handle == submission_handle)
            .distinct()
            .order_by(FormVersion.version.asc())
        ).all()
        return [{"id": r[0], "version": r[1]} for r in rows]


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


def load_submissions() -> list[dict[str, Any]]:
    """Read every persisted submission back out, as plain dicts the
    runtime rehydrates into its in-memory types. Same shape as the
    `sync_submission` snapshot."""
    out: list[dict[str, Any]] = []
    with Session(_engine) as session:
        for sub in session.scalars(select(Submission)):
            out.append(
                {
                    "handle": sub.handle,
                    "submission_id": sub.submission_id,
                    "form_version_id": sub.form_version_id,
                    "state": sub.state,
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

        # Submission counts grouped by (form, state).
        counts: dict[str, dict[str, int]] = {}
        for form_id, state, n in session.execute(
            select(
                FormVersion.form_id,
                Submission.state,
                func.count(Submission.handle),
            )
            .join(Submission, Submission.form_version_id == FormVersion.id)
            .group_by(FormVersion.form_id, Submission.state)
        ).all():
            counts.setdefault(form_id, {})[state] = n

        # Last activity = the most recent event of any of the form's
        # submissions (truthful even for still-running submissions).
        # We also keep the submission's id so the form-summary KPI can
        # link directly to the submission that produced the timestamp.
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


def list_form_submissions(form_id: str) -> list[dict[str, Any]]:
    """Every submission of a form, newest first — state, timing, the
    version it ran on, and the node id of its current step."""
    with Session(_engine) as session:
        rows = session.execute(
            select(Submission, FormVersion.version)
            .join(FormVersion, Submission.form_version_id == FormVersion.id)
            .where(FormVersion.form_id == form_id)
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
                "terminated_at": _aware(sub.terminated_at),
                "current_step": sub.steps[-1].node_id if sub.steps else None,
            }
            for sub, version in rows
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
