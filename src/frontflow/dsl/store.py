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
    """
    explicit = os.environ.get("DATABASE_URL")
    if explicit:
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

    steps: Mapped[list["Step"]] = relationship(
        back_populates="submission",
        order_by="Step.seq",
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
    button_clicked: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    next_node_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    branch_explicit: Mapped[bool] = mapped_column(Boolean, default=False)

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


def first_aws_connection() -> Optional[dict[str, Any]]:
    """The first stored AWS connection's decrypted credential payload,
    or None if none is configured. Used by S3File uploads to resolve
    credentials before falling back to boto3's default chain.

    The secret dict carries: aws_access_key_id, aws_secret_access_key,
    optional aws_session_token, region, and an optional default
    bucket. As with get_connection, the secret is sensitive — callers
    must not serialize it back over the API."""
    with Session(_engine) as session:
        c = session.scalars(
            select(Connection)
            .where(Connection.conn_type == "aws")
            .order_by(Connection.name)
        ).first()
        if c is None:
            return None
        return {
            "name": c.name,
            "base_url": c.base_url,
            "secret": decrypt_secret(c.secret),
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
) -> int:
    """Record a form and its current compiled state. Inserts a new
    form_version only when the content hash differs from the latest.
    Returns the id of the form_version that is now current."""
    with Session(_engine) as session:
        form = session.get(Form, form_id)
        now = _utcnow()
        if form is None:
            form = Form(
                form_id=form_id,
                name=name,
                folder_path=folder_path,
                is_live=True,
                created_at=now,
                updated_at=now,
            )
            session.add(form)
        else:
            form.name = name
            form.folder_path = folder_path
            form.is_live = True
            form.updated_at = now

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

        # Steps mirror the current chain — drop and rewrite.
        for existing in list(sub.steps):
            session.delete(existing)
        sub.steps = [
            Step(
                submission_handle=sub.handle,
                seq=s["seq"],
                node_id=s["node_id"],
                page_id=s["page_id"],
                kind=s["kind"],
                state=s["state"],
                started_at=s["started_at"],
                submitted_at=s["submitted_at"],
                form_values=s["form_values"],
                backend_return=s["backend_return"],
                button_clicked=s["button_clicked"],
                next_node_id=s["next_node_id"],
                branch_explicit=s["branch_explicit"],
            )
            for s in snapshot["steps"]
        ]

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
                    occurred_at=e["occurred_at"],
                    payload=e["payload"],
                )
            )

        session.commit()


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
                            "button_clicked": s.button_clicked,
                            "next_node_id": s.next_node_id,
                            "branch_explicit": s.branch_explicit,
                        }
                        for s in sub.steps
                    ],
                    "events": [
                        {
                            "type": e.type,
                            "node_id": e.node_id,
                            "page_id": e.page_id,
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
        last_activity = {
            form_id: _aware(ts)
            for form_id, ts in session.execute(
                select(FormVersion.form_id, func.max(Event.occurred_at))
                .join(Submission, Submission.form_version_id == FormVersion.id)
                .join(Event, Event.submission_handle == Submission.handle)
                .group_by(FormVersion.form_id)
            ).all()
        }

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
            _export_record(sub, version, fid)
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
    sub: "Submission", version: int, form_id: str
) -> dict[str, Any]:
    """One submission as an export record — identity, lifecycle, and a
    compact per-step breakdown carrying the submitted form values."""
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
        ],
    }


def _iso(dt: Optional[datetime]) -> Optional[str]:
    """A timezone-aware ISO 8601 string, or None."""
    aware = _aware(dt)
    return aware.isoformat() if aware is not None else None
