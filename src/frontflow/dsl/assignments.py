"""Submission assignment — CRUD + active-state queries.

Phase 4 of the role-based assignment system. The runtime layer
over `SubmissionAssignment`. Two callers:

  - The `Assign` operator (creates assignments programmatically
    when a parent submission's node fires).
  - The admin UI / API (creates assignments manually; lists,
    revokes).

Plus a read-side helper used by the auth check:

  - `active_roles_for_user_on_submission(submission_handle, user_id)`
    returns the frozenset of role identifiers the user currently
    has on the submission. Drives the per-request permission
    decision; cheap by design (indexed lookup, no joins beyond
    the row itself).

Assignment rows are append-only: a re-grant after revocation
inserts a NEW row. Historical access windows survive in the
audit trail.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import and_, select
from sqlalchemy.orm import Session as DBSession

from .store import SubmissionAssignment, _engine, _utcnow


def grant(
    *,
    submission_handle: str,
    user_id: int,
    role_id: str,
    granted_by_user_id: int,
    granted_by_submission_handle: Optional[str] = None,
) -> SubmissionAssignment:
    """Create a new assignment row.

    Idempotent: if the (submission, user, role) triple already has
    an ACTIVE row (revoked_at IS NULL), return that row instead of
    inserting a duplicate. A re-grant after revocation DOES insert
    a new row — the revocation window is preserved in audit.

    Raises ValueError if `role_id` is empty.
    """
    if not role_id:
        raise ValueError("role_id must be a non-empty string")
    with DBSession(_engine) as db:
        existing = db.execute(
            select(SubmissionAssignment).where(
                SubmissionAssignment.submission_handle == submission_handle,
                SubmissionAssignment.user_id == user_id,
                SubmissionAssignment.role_id == role_id,
                SubmissionAssignment.revoked_at.is_(None),
            )
        ).scalar_one_or_none()
        if existing is not None:
            return _detach(existing)
        row = SubmissionAssignment(
            submission_handle=submission_handle,
            user_id=user_id,
            role_id=role_id,
            granted_at=_utcnow(),
            granted_by_user_id=granted_by_user_id,
            granted_by_submission_handle=granted_by_submission_handle,
            revoked_at=None,
            revoked_by_user_id=None,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return _detach(row)


def revoke(
    *,
    assignment_id: int,
    revoked_by_user_id: int,
) -> Optional[SubmissionAssignment]:
    """Revoke an active assignment by id. Sets revoked_at + the
    revoker. Returns the updated row, or None if no row matches
    or it's already revoked. Fires the child form's on_revoked
    hook if registered (failures are logged + swallowed).
    """
    with DBSession(_engine) as db:
        row = db.get(SubmissionAssignment, assignment_id)
        if row is None or row.revoked_at is not None:
            return None
        row.revoked_at = _utcnow()
        row.revoked_by_user_id = revoked_by_user_id
        db.commit()
        db.refresh(row)
        detached = _detach(row)
    # Fire hook AFTER commit + session close. The hook may run
    # arbitrary code; we don't want it holding the DB session.
    _fire_on_revoked_hook(detached)
    return detached


def revoke_all_for_user_on_submission(
    *,
    submission_handle: str,
    user_id: int,
    revoked_by_user_id: int,
) -> int:
    """Revoke every active assignment for the user on the
    submission. Used by edit-cascade (assignee changed → clear
    downstream) and by external-system deactivation. Returns the
    count of rows affected.

    Fires on_revoked once per row (after commit). The N-call cost
    is tolerated because revocation is rare and the hook is
    per-form, not per-row.
    """
    with DBSession(_engine) as db:
        rows = db.execute(
            select(SubmissionAssignment).where(
                SubmissionAssignment.submission_handle == submission_handle,
                SubmissionAssignment.user_id == user_id,
                SubmissionAssignment.revoked_at.is_(None),
            )
        ).scalars().all()
        now = _utcnow()
        for r in rows:
            r.revoked_at = now
            r.revoked_by_user_id = revoked_by_user_id
        db.commit()
        detached = [_detach(r) for r in rows]
    for r in detached:
        _fire_on_revoked_hook(r)
    return len(rows)


def revoke_all_for_user(
    *,
    user_id: int,
    revoked_by_user_id: int,
) -> int:
    """Revoke every active assignment for the user, across all
    submissions. Used when an external user is deactivated.
    Returns the count of rows affected.
    """
    with DBSession(_engine) as db:
        rows = db.execute(
            select(SubmissionAssignment).where(
                SubmissionAssignment.user_id == user_id,
                SubmissionAssignment.revoked_at.is_(None),
            )
        ).scalars().all()
        now = _utcnow()
        for r in rows:
            r.revoked_at = now
            r.revoked_by_user_id = revoked_by_user_id
        db.commit()
        detached = [_detach(r) for r in rows]
    for r in detached:
        _fire_on_revoked_hook(r)
    return len(rows)


def _fire_on_revoked_hook(row: SubmissionAssignment) -> None:
    """Look up the form holding the now-revoked submission's role
    template and invoke its on_revoked hook. Failures are logged
    + swallowed."""
    # Resolve form_id by joining submission → form_version → form_id.
    from .store import (
        FormVersion, Submission as _SubmissionRow, User,
    )
    from frontflow.dsl.core import WORKFLOWS

    with DBSession(_engine) as db:
        sub = db.get(_SubmissionRow, row.submission_handle)
        if sub is None:
            return
        version = db.get(FormVersion, sub.form_version_id)
        if version is None:
            return
        form_id = version.form_id
        # Look up the assignee + revoker usernames for the event.
        assignee = db.get(User, row.user_id)
        revoker = (
            db.get(User, row.revoked_by_user_id)
            if row.revoked_by_user_id is not None
            else None
        )

    wf = WORKFLOWS.get(form_id)
    if wf is None:
        return
    hook = getattr(wf, "on_revoked", None)
    if hook is None:
        return
    event = {
        "kind": "revoked",
        "form_id": form_id,
        "submission_handle": row.submission_handle,
        "assignment_id": row.id,
        "assignee_user_id": row.user_id,
        "assignee_username": assignee.username if assignee else None,
        "role_id": row.role_id,
        "revoked_by_user_id": row.revoked_by_user_id,
        "revoked_by_username": revoker.username if revoker else None,
        "revoked_at": (
            row.revoked_at.isoformat() if row.revoked_at else None
        ),
    }
    try:
        hook(event)
    except Exception as e:  # noqa: BLE001 — never let a hook break revoke
        print(f"[on_revoked] hook for {form_id!r} raised: {e}")


def active_roles_for_user_on_submission(
    submission_handle: str, user_id: int,
) -> frozenset[str]:
    """Return the role identifiers the user is currently assigned
    on the submission. Frozen set so callers can use it as a key
    in the runtime auth check without worrying about mutation.

    O(1) under the (submission_handle, user_id, revoked_at) index.
    """
    with DBSession(_engine) as db:
        rows = db.execute(
            select(SubmissionAssignment.role_id).where(
                SubmissionAssignment.submission_handle == submission_handle,
                SubmissionAssignment.user_id == user_id,
                SubmissionAssignment.revoked_at.is_(None),
            )
        ).scalars().all()
        return frozenset(rows)


def list_active_for_submission(
    submission_handle: str,
) -> list[dict[str, Any]]:
    """Every active assignment on a submission. For the
    submission-detail UI's "who's working on this" panel.
    Returns a list of dicts so the JSON shape doesn't leak
    SQLAlchemy types.
    """
    with DBSession(_engine) as db:
        rows = db.execute(
            select(SubmissionAssignment).where(
                SubmissionAssignment.submission_handle == submission_handle,
                SubmissionAssignment.revoked_at.is_(None),
            ).order_by(SubmissionAssignment.granted_at)
        ).scalars().all()
        return [_to_dict(r) for r in rows]


def list_history_for_submission(
    submission_handle: str,
) -> list[dict[str, Any]]:
    """Every assignment ever granted on the submission, active
    AND revoked, ordered by granted_at. The audit log surface."""
    with DBSession(_engine) as db:
        rows = db.execute(
            select(SubmissionAssignment).where(
                SubmissionAssignment.submission_handle == submission_handle,
            ).order_by(SubmissionAssignment.granted_at)
        ).scalars().all()
        return [_to_dict(r) for r in rows]


def list_active_for_user(user_id: int) -> list[dict[str, Any]]:
    """Every active assignment for a user, across submissions.
    Drives the `/my-tasks` inbox query.
    """
    with DBSession(_engine) as db:
        rows = db.execute(
            select(SubmissionAssignment).where(
                SubmissionAssignment.user_id == user_id,
                SubmissionAssignment.revoked_at.is_(None),
            ).order_by(SubmissionAssignment.granted_at.desc())
        ).scalars().all()
        return [_to_dict(r) for r in rows]


def _detach(row: SubmissionAssignment) -> SubmissionAssignment:
    """A session-free copy of a row, safe to return after the DB
    session closes."""
    return SubmissionAssignment(
        id=row.id,
        submission_handle=row.submission_handle,
        user_id=row.user_id,
        role_id=row.role_id,
        granted_at=row.granted_at,
        granted_by_user_id=row.granted_by_user_id,
        granted_by_submission_handle=row.granted_by_submission_handle,
        revoked_at=row.revoked_at,
        revoked_by_user_id=row.revoked_by_user_id,
    )


def _to_dict(row: SubmissionAssignment) -> dict[str, Any]:
    return {
        "id": row.id,
        "submission_handle": row.submission_handle,
        "user_id": row.user_id,
        "role_id": row.role_id,
        "granted_at": row.granted_at.isoformat() if row.granted_at else None,
        "granted_by_user_id": row.granted_by_user_id,
        "granted_by_submission_handle": row.granted_by_submission_handle,
        "revoked_at": (
            row.revoked_at.isoformat() if row.revoked_at else None
        ),
        "revoked_by_user_id": row.revoked_by_user_id,
    }
