"""Authentication — password hashing and server-side sessions.

Milestone 1 of frontflow's auth: hosted accounts. Passwords are hashed
with argon2id; login sessions are opaque tokens stored in the database
(so they survive a restart and can be revoked).

The data model (User, AuthSession) lives in store.py; this module is the
behavior layer over it. It is kept identity-source-agnostic on purpose
— a later SSO integration resolves to the same User rows and issues
sessions the same way, without touching the authorization layer.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from .store import (
    AuthSession,
    FolderGrant,
    Form,
    FormACL,
    Group,
    User,
    UserGroup,
    _engine,
)

# Session lifetime, and the threshold past which an authenticated
# request slides the expiry forward (renewal at the halfway mark).
SESSION_LIFETIME = timedelta(days=7)
_RENEW_AFTER = SESSION_LIFETIME / 2

_hasher = PasswordHasher()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """A naive datetime read back from the DB is UTC — make it aware."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# --- Passwords -------------------------------------------------------------


def hash_password(password: str) -> str:
    """Hash a password with argon2id for storage."""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Check a password against a stored hash. False on any mismatch."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError):
        return False


# --- Users -----------------------------------------------------------------


def any_users_exist() -> bool:
    """Whether any account exists at all. When none do, the admin
    surface stays closed (503) rather than running open."""
    with DBSession(_engine) as db:
        return db.scalar(select(User.id).limit(1)) is not None


def _detach(user: User) -> User:
    """A plain, session-free copy of a User row, safe to return after
    the DB session closes. Carries every field callers may read."""
    return User(
        id=user.id,
        username=user.username,
        is_admin=user.is_admin,
        is_active=user.is_active,
        must_change_password=user.must_change_password,
        created_at=user.created_at,
        external_id=user.external_id,
        notification_preferences=(
            dict(user.notification_preferences)
            if user.notification_preferences is not None
            else {}
        ),
    )


def create_user(
    username: str, password: str, *, is_admin: bool = False
) -> User:
    """Create an account. Raises ValueError if the username is taken."""
    username = username.strip()
    if not username:
        raise ValueError("username cannot be empty")
    with DBSession(_engine) as db:
        existing = db.scalar(
            select(User).where(User.username == username)
        )
        if existing is not None:
            raise ValueError(f"user {username!r} already exists")
        user = User(
            username=username,
            password_hash=hash_password(password),
            is_admin=is_admin,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return _detach(user)


def get_user_by_username(username: str) -> Optional[User]:
    """Look up a user by username. Returns None if no match."""
    username = username.strip()
    if not username:
        return None
    with DBSession(_engine) as db:
        user = db.scalar(
            select(User).where(User.username == username)
        )
        return _detach(user) if user is not None else None


def authenticate(username: str, password: str) -> Optional[User]:
    """Return the user if the username/password match, else None. A
    deactivated account cannot authenticate."""
    with DBSession(_engine) as db:
        user = db.scalar(
            select(User).where(User.username == username)
        )
        if user is None or user.password_hash is None:
            return None
        if not user.is_active:
            return None
        if not verify_password(user.password_hash, password):
            return None
        return _detach(user)


# --- Sessions --------------------------------------------------------------


def create_session(user_id: int) -> str:
    """Open a session for a user; returns the opaque session token."""
    token = secrets.token_urlsafe(32)
    now = _utcnow()
    with DBSession(_engine) as db:
        db.add(
            AuthSession(
                token=token,
                user_id=user_id,
                created_at=now,
                expires_at=now + SESSION_LIFETIME,
            )
        )
        db.commit()
    return token


def resolve_session(token: Optional[str]) -> Optional[User]:
    """Resolve a session token to its user.

    Returns None if the token is absent, unknown, or expired. A live
    session past its halfway point has its expiry slid forward, so an
    active operator is not logged out mid-session.
    """
    if not token:
        return None
    now = _utcnow()
    with DBSession(_engine) as db:
        sess = db.get(AuthSession, token)
        if sess is None:
            return None
        if _aware(sess.expires_at) <= now:
            # Expired — clean it up and reject.
            db.delete(sess)
            db.commit()
            return None
        user = db.get(User, sess.user_id)
        if user is None:
            return None
        if not user.is_active:
            # Deactivated mid-session — reject and clean up.
            db.delete(sess)
            db.commit()
            return None
        # Sliding renewal: extend if past the halfway mark.
        if now - _aware(sess.created_at) > _RENEW_AFTER:
            sess.created_at = now
            sess.expires_at = now + SESSION_LIFETIME
            db.commit()
        return _detach(user)


def revoke_session(token: Optional[str]) -> None:
    """End a session (logout). A no-op if the token is unknown."""
    if not token:
        return
    with DBSession(_engine) as db:
        sess = db.get(AuthSession, token)
        if sess is not None:
            db.delete(sess)
            db.commit()


# --- Folder authorization (M2) ---------------------------------------------
#
# A group is granted a role on a folder path; the grant cascades to
# every form under that path. A user's access to a form is the highest
# role any of their groups' grants confer on it. Admins bypass all of
# it. The form-filling routes are unaffected — this gates the console.

# Role ordering — a higher value supersedes a lower one.
_ROLE_RANK = {"view": 1, "manage": 2}


def _path_covers(grant_path: str, form_path: str) -> bool:
    """Whether a grant on `grant_path` covers a form in `form_path`.

    A grant cascades down its subtree: the root ("") covers everything;
    "billing" covers "billing" and "billing/x" but NOT "billing-archive"
    — the boundary is a real path segment, not a bare string prefix.
    """
    if grant_path == "":
        return True
    return form_path == grant_path or form_path.startswith(
        grant_path + "/"
    )


def _higher_role(a: Optional[str], b: str) -> str:
    """Return whichever of two roles confers more access."""
    if a is None:
        return b
    return a if _ROLE_RANK[a] >= _ROLE_RANK[b] else b


def user_form_access(
    user: User, form_folder: str
) -> Optional[str]:
    """The role a user has on a form, by the form's folder path.

    Returns 'manage', 'view', or None (no access). An admin always has
    'manage'. Otherwise it is the highest role any grant held by any of
    the user's groups confers on the form's folder.
    """
    if user.is_admin:
        return "manage"
    with DBSession(_engine) as db:
        grants = db.execute(
            select(FolderGrant.folder_path, FolderGrant.role)
            .join(UserGroup, UserGroup.group_id == FolderGrant.group_id)
            .where(UserGroup.user_id == user.id)
        ).all()
    best: Optional[str] = None
    for grant_path, role in grants:
        if _path_covers(grant_path, form_folder):
            best = _higher_role(best, role)
    return best


def accessible_form_folders(user: User) -> Optional[list[str]]:
    """The set of granted folder paths for a user, for filtering the
    forms list. Returns None when the user can see everything (an
    admin, or a group with a root grant) — the caller then skips
    filtering. A non-admin with no grants gets an empty list.
    """
    if user.is_admin:
        return None
    with DBSession(_engine) as db:
        paths = db.execute(
            select(FolderGrant.folder_path)
            .join(UserGroup, UserGroup.group_id == FolderGrant.group_id)
            .where(UserGroup.user_id == user.id)
        ).scalars().all()
    if any(p == "" for p in paths):
        return None  # a root grant — sees everything
    return sorted(set(paths))


def folder_is_accessible(
    granted: Optional[list[str]], form_folder: str
) -> bool:
    """Whether a form's folder is covered by a granted-paths list (the
    output of accessible_form_folders). None means unrestricted."""
    if granted is None:
        return True
    return any(_path_covers(g, form_folder) for g in granted)


# --- Groups & grants (admin management) ------------------------------------


def list_groups() -> list[dict]:
    """All groups, each with its member count and grant count."""
    with DBSession(_engine) as db:
        out = []
        for g in db.scalars(select(Group).order_by(Group.name)):
            members = db.scalar(
                select(func.count())
                .select_from(UserGroup)
                .where(UserGroup.group_id == g.id)
            )
            grants = db.scalar(
                select(func.count())
                .select_from(FolderGrant)
                .where(FolderGrant.group_id == g.id)
            )
            out.append(
                {
                    "id": g.id,
                    "name": g.name,
                    "member_count": members or 0,
                    "grant_count": grants or 0,
                }
            )
        return out


def create_group(name: str) -> dict:
    """Create a group. Raises ValueError if the name is taken."""
    name = name.strip()
    if not name:
        raise ValueError("group name cannot be empty")
    with DBSession(_engine) as db:
        if db.scalar(select(Group).where(Group.name == name)):
            raise ValueError(f"group {name!r} already exists")
        g = Group(name=name)
        db.add(g)
        db.commit()
        db.refresh(g)
        return {"id": g.id, "name": g.name}


def delete_group(group_id: int) -> None:
    """Delete a group and its memberships and grants."""
    with DBSession(_engine) as db:
        g = db.get(Group, group_id)
        if g is None:
            raise ValueError("group not found")
        db.execute(
            UserGroup.__table__.delete().where(
                UserGroup.group_id == group_id
            )
        )
        db.execute(
            FolderGrant.__table__.delete().where(
                FolderGrant.group_id == group_id
            )
        )
        db.delete(g)
        db.commit()


def group_detail(group_id: int) -> dict:
    """A group with its members and folder grants."""
    with DBSession(_engine) as db:
        g = db.get(Group, group_id)
        if g is None:
            raise ValueError("group not found")
        member_rows = db.execute(
            select(User.id, User.username)
            .join(UserGroup, UserGroup.user_id == User.id)
            .where(UserGroup.group_id == group_id)
            .order_by(User.username)
        ).all()
        grant_rows = db.scalars(
            select(FolderGrant)
            .where(FolderGrant.group_id == group_id)
            .order_by(FolderGrant.folder_path)
        ).all()
        return {
            "id": g.id,
            "name": g.name,
            "members": [
                {"id": uid, "username": un}
                for uid, un in member_rows
            ],
            "grants": [
                {
                    "id": gr.id,
                    "folder_path": gr.folder_path,
                    "role": gr.role,
                }
                for gr in grant_rows
            ],
        }


def add_member(group_id: int, user_id: int) -> None:
    """Add a user to a group. Idempotent."""
    with DBSession(_engine) as db:
        if db.get(Group, group_id) is None:
            raise ValueError("group not found")
        if db.get(User, user_id) is None:
            raise ValueError("user not found")
        exists = db.get(UserGroup, {"user_id": user_id,
                                    "group_id": group_id})
        if exists is None:
            db.add(UserGroup(user_id=user_id, group_id=group_id))
            db.commit()


def remove_member(group_id: int, user_id: int) -> None:
    """Remove a user from a group. A no-op if not a member."""
    with DBSession(_engine) as db:
        m = db.get(UserGroup, {"user_id": user_id,
                               "group_id": group_id})
        if m is not None:
            db.delete(m)
            db.commit()


def add_grant(group_id: int, folder_path: str, role: str) -> dict:
    """Grant a group a role on a folder subtree. `folder_path` is
    accepted as-is — folders are emergent from forms, so a grant may
    name a folder no form lives in yet."""
    if role not in _ROLE_RANK:
        raise ValueError(f"role must be 'view' or 'manage', not {role!r}")
    folder_path = folder_path.strip().strip("/")
    with DBSession(_engine) as db:
        if db.get(Group, group_id) is None:
            raise ValueError("group not found")
        gr = FolderGrant(
            group_id=group_id, folder_path=folder_path, role=role
        )
        db.add(gr)
        db.commit()
        db.refresh(gr)
        return {
            "id": gr.id,
            "folder_path": gr.folder_path,
            "role": gr.role,
        }


def remove_grant(grant_id: int) -> None:
    """Delete a folder grant."""
    with DBSession(_engine) as db:
        gr = db.get(FolderGrant, grant_id)
        if gr is not None:
            db.delete(gr)
            db.commit()


def list_users() -> list[dict]:
    """All user accounts — for assigning group membership and for the
    user-management console."""
    with DBSession(_engine) as db:
        return [
            {
                "id": u.id,
                "username": u.username,
                "is_admin": u.is_admin,
                "is_active": u.is_active,
                "must_change_password": u.must_change_password,
            }
            for u in db.scalars(
                select(User).order_by(User.username)
            )
        ]


# --- Per-form visibility (M3) ----------------------------------------------
#
# A form's visibility governs who may reach the form-FILLING surface
# (the console is governed by folder grants, M2). The check is additive:
# a folder grant or admin always confers access, on top of whatever the
# visibility mode allows.


def can_access_form(
    user: Optional[User],
    form_id: str,
    unlisted_token: Optional[str] = None,
) -> bool:
    """Whether a visitor may reach a form's filling surface.

    `user` is the signed-in user or None. `unlisted_token` is the key
    supplied on the request (query param), if any.

    Rules, additive:
      - public      → anyone.
      - unlisted    → anyone presenting the matching token.
      - restricted  → a signed-in user on the form's ACL.
      - ANY mode    → an admin, or a user a folder grant covers, always
                      gets in (folder access supersedes visibility).
    """
    with DBSession(_engine) as db:
        form = db.get(Form, form_id)
        if form is None:
            return False
        visibility = form.visibility or "public"
        folder = form.folder_path or ""
        token = form.unlisted_token

        # Additive: a folder grant (or admin) always confers access.
        if user is not None and user_form_access(user, folder) is not None:
            return True

        if visibility == "public":
            return True
        if visibility == "unlisted":
            return (
                unlisted_token is not None
                and token is not None
                and secrets.compare_digest(unlisted_token, token)
            )
        if visibility == "restricted":
            if user is None:
                return False
            acl = db.get(
                FormACL, {"form_id": form_id, "user_id": user.id}
            )
            return acl is not None
        # Unknown mode — fail closed.
        return False


def can_access_workspace(
    user: Optional[User],
    workspace_id: str,
    unlisted_token: Optional[str] = None,
) -> bool:
    """Whether a visitor may open a workspace.

    Same three-way model as forms — public / unlisted / restricted, with
    admins always admitted. Deliberately mirrors `can_access_form` so
    there is one access model to reason about rather than two.

    This is the gate on a workspace's DASHBOARD panels. A dashboard
    inside a form borrows that form's ACL; a dashboard in a workspace has
    no form to borrow from, so this decides. Unknown visibility fails
    closed.

    Note what this does NOT do: it does not widen access to the FORMS a
    workspace contains. A form panel is still subject to the form's own
    visibility, so putting a restricted form in a public workspace does
    not publish that form.
    """
    from . import store as _store  # local: mirrors this module's style

    if user is not None and getattr(user, "is_admin", False):
        return True

    ws = _store.get_workspace(workspace_id)
    if ws is None:
        return False

    visibility = ws.get("visibility") or "public"

    if visibility == "public":
        return True
    if visibility == "unlisted":
        token = ws.get("unlisted_token")
        return (
            token is not None
            and unlisted_token is not None
            and secrets.compare_digest(token, unlisted_token)
        )
    if visibility == "restricted":
        if user is None:
            return False
        return user.id in _store.workspace_acl_user_ids(workspace_id)
    # Unknown mode — fail closed.
    return False


def _is_dsl_locked(form_id: str) -> bool:
    """True if the form's DSL declares visibility (e.g. via
    `@form(private=True)`) — admin UI cannot override DSL-declared
    settings.

    Read from the live in-memory workflow registry, not the DB, so
    the answer reflects the source as of the latest scan. A form
    not currently in the registry (deleted source, broken file) is
    treated as unlocked — there's no DSL to defer to.
    """
    # Lazy import — avoids `dsl.auth` ↔ `main` circular at module
    # load. `main` already imports `auth`; importing `main` back
    # from inside a function lets the runtime resolve cleanly.
    try:
        from frontflow.main import WORKFLOWS
    except ImportError:  # pragma: no cover — defensive
        return False
    wf = WORKFLOWS.get(form_id)
    if wf is None:
        return False
    return bool(getattr(wf, "private", False))


class FormVisibilityLocked(ValueError):
    """Raised when an admin tries to change a visibility value that
    is declared in the form's DSL. The DSL is the source of truth;
    admins edit the form file (and commit) to change it."""


def get_form_visibility(form_id: str) -> Optional[dict]:
    """A form's visibility settings, or None if no such form.
    Includes the unlisted token, the restricted ACL usernames, and
    whether the visibility is locked by the form's DSL (the admin
    UI should render the visibility controls read-only in that
    case)."""
    with DBSession(_engine) as db:
        form = db.get(Form, form_id)
        if form is None:
            return None
        acl_rows = db.execute(
            select(User.id, User.username)
            .join(FormACL, FormACL.user_id == User.id)
            .where(FormACL.form_id == form_id)
            .order_by(User.username)
        ).all()
        return {
            "visibility": form.visibility or "public",
            "unlisted_token": form.unlisted_token,
            "acl": [
                {"id": uid, "username": un} for uid, un in acl_rows
            ],
            "dsl_locked": _is_dsl_locked(form_id),
        }


def set_form_visibility(form_id: str, visibility: str) -> None:
    """Set a form's visibility mode. Going unlisted mints a token if
    the form has none yet; other modes leave any existing token in
    place (so toggling back to unlisted keeps the same link).

    Raises FormVisibilityLocked when the form's DSL declares
    visibility — the source is the source of truth, and the admin
    UI does not override settings declared in code. To change a
    DSL-locked form's visibility, edit the form source.
    """
    if visibility not in ("public", "unlisted", "restricted"):
        raise ValueError(f"unknown visibility {visibility!r}")
    if _is_dsl_locked(form_id):
        raise FormVisibilityLocked(
            f"form {form_id!r}'s visibility is declared in its DSL "
            "(`@form(private=True)`). Edit the form source to change "
            "it; the admin UI cannot override settings declared in "
            "code."
        )
    with DBSession(_engine) as db:
        form = db.get(Form, form_id)
        if form is None:
            raise ValueError("form not found")
        form.visibility = visibility
        if visibility == "unlisted" and not form.unlisted_token:
            form.unlisted_token = secrets.token_urlsafe(24)
        db.commit()


def regenerate_unlisted_token(form_id: str) -> str:
    """Mint a fresh unlisted token, invalidating any existing link."""
    with DBSession(_engine) as db:
        form = db.get(Form, form_id)
        if form is None:
            raise ValueError("form not found")
        form.unlisted_token = secrets.token_urlsafe(24)
        token = form.unlisted_token
        db.commit()
    return token


def add_form_acl(form_id: str, user_id: int) -> None:
    """Permit a user on a restricted form. Idempotent."""
    with DBSession(_engine) as db:
        if db.get(Form, form_id) is None:
            raise ValueError("form not found")
        if db.get(User, user_id) is None:
            raise ValueError("user not found")
        existing = db.get(
            FormACL, {"form_id": form_id, "user_id": user_id}
        )
        if existing is None:
            db.add(FormACL(form_id=form_id, user_id=user_id))
            db.commit()


def remove_form_acl(form_id: str, user_id: int) -> None:
    """Remove a user from a restricted form's allow-list."""
    with DBSession(_engine) as db:
        acl = db.get(
            FormACL, {"form_id": form_id, "user_id": user_id}
        )
        if acl is not None:
            db.delete(acl)
            db.commit()


# --- User management -------------------------------------------------------
#
# Admin operations over accounts. Two invariants are enforced here, not
# left to the caller:
#   - The last active admin cannot be demoted, deactivated, or deleted
#     (it would lock everyone out of the console).
#   - Any security-reducing change revokes the target's live sessions,
#     so it takes effect immediately rather than at cookie expiry.


def revoke_user_sessions(user_id: int) -> int:
    """End every live session for a user. Returns how many were ended."""
    with DBSession(_engine) as db:
        sessions = db.scalars(
            select(AuthSession).where(AuthSession.user_id == user_id)
        ).all()
        n = len(sessions)
        for s in sessions:
            db.delete(s)
        db.commit()
        return n


def count_active_admins() -> int:
    """Number of accounts that are both admin and active."""
    with DBSession(_engine) as db:
        return (
            db.scalar(
                select(func.count())
                .select_from(User)
                .where(User.is_admin.is_(True))
                .where(User.is_active.is_(True))
            )
            or 0
        )


def _is_last_active_admin(db, user: User) -> bool:
    """Whether this user is the only active admin left — used to block
    an action that would otherwise leave zero admins."""
    if not (user.is_admin and user.is_active):
        return False
    others = (
        db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.is_admin.is_(True))
            .where(User.is_active.is_(True))
            .where(User.id != user.id)
        )
        or 0
    )
    return others == 0


def set_user_password(user_id: int, new_password: str) -> None:
    """Admin reset of another user's password. Sets the
    must-change-password flag and revokes the user's sessions, so they
    must log in afresh with the temporary password and change it."""
    if not new_password:
        raise ValueError("password cannot be empty")
    with DBSession(_engine) as db:
        user = db.get(User, user_id)
        if user is None:
            raise ValueError("user not found")
        user.password_hash = hash_password(new_password)
        user.must_change_password = True
        db.commit()
    revoke_user_sessions(user_id)


def change_own_password(
    user_id: int, current_password: str, new_password: str
) -> None:
    """A user changing their own password. Verifies the current
    password first. Clears the must-change flag. Other sessions are
    left alone — this is the user's own deliberate action."""
    if not new_password:
        raise ValueError("password cannot be empty")
    with DBSession(_engine) as db:
        user = db.get(User, user_id)
        if user is None:
            raise ValueError("user not found")
        if user.password_hash is None or not verify_password(
            user.password_hash, current_password
        ):
            raise PermissionError("current password is incorrect")
        user.password_hash = hash_password(new_password)
        user.must_change_password = False
        db.commit()


def set_user_admin(user_id: int, is_admin: bool) -> None:
    """Promote or demote a user. Demoting the last active admin is
    refused. A demotion revokes the user's sessions."""
    with DBSession(_engine) as db:
        user = db.get(User, user_id)
        if user is None:
            raise ValueError("user not found")
        if not is_admin and _is_last_active_admin(db, user):
            raise ValueError(
                "cannot demote the last admin — promote another "
                "account first"
            )
        was_admin = user.is_admin
        user.is_admin = is_admin
        db.commit()
    if was_admin and not is_admin:
        revoke_user_sessions(user_id)


def set_user_active(user_id: int, is_active: bool) -> None:
    """Activate or deactivate an account. Deactivating the last active
    admin is refused. Deactivation revokes the user's sessions."""
    with DBSession(_engine) as db:
        user = db.get(User, user_id)
        if user is None:
            raise ValueError("user not found")
        if not is_active and _is_last_active_admin(db, user):
            raise ValueError(
                "cannot deactivate the last admin — promote another "
                "account first"
            )
        user.is_active = is_active
        db.commit()
    if not is_active:
        revoke_user_sessions(user_id)


def delete_user(user_id: int) -> None:
    """Hard-delete an account and its group memberships, form-ACL
    entries, and sessions. Deleting the last active admin is refused."""
    with DBSession(_engine) as db:
        user = db.get(User, user_id)
        if user is None:
            raise ValueError("user not found")
        if _is_last_active_admin(db, user):
            raise ValueError(
                "cannot delete the last admin — promote another "
                "account first"
            )
        db.execute(
            UserGroup.__table__.delete().where(
                UserGroup.user_id == user_id
            )
        )
        db.execute(
            FormACL.__table__.delete().where(
                FormACL.user_id == user_id
            )
        )
        db.execute(
            AuthSession.__table__.delete().where(
                AuthSession.user_id == user_id
            )
        )
        db.delete(user)
        db.commit()


# --- Notification preferences ---------------------------------------------


def get_notification_preferences(user_id: int) -> Optional[dict]:
    """Returns the user's notification preferences dict, or None
    if no user with that id exists. An empty dict is a valid
    return — it means "no opt-outs set"; handlers default to
    sending. Distinct from None (user not found)."""
    with DBSession(_engine) as db:
        user = db.get(User, user_id)
        if user is None:
            return None
        return dict(user.notification_preferences or {})


def set_notification_preferences(
    user_id: int, preferences: dict
) -> dict:
    """Overwrite a user's notification preferences. The whole dict
    is replaced — pass the complete desired state. Values must be
    booleans; channel keys are open-ended strings (no enum). Raises
    ValueError when the user does not exist or values are
    malformed."""
    if not isinstance(preferences, dict):
        raise ValueError("preferences must be a dict")
    for k, v in preferences.items():
        if not isinstance(k, str):
            raise ValueError(
                f"channel name must be a string, got "
                f"{type(k).__name__}"
            )
        if not isinstance(v, bool):
            raise ValueError(
                f"channel {k!r} value must be a bool, got "
                f"{type(v).__name__}"
            )
    from sqlalchemy.orm.attributes import flag_modified
    with DBSession(_engine) as db:
        user = db.get(User, user_id)
        if user is None:
            raise ValueError(f"no user with id {user_id}")
        user.notification_preferences = dict(preferences)
        # JSON columns store the dict by reference; SQLAlchemy may
        # not detect in-place mutations. Force change-detection so
        # the new dict is persisted. Belt-and-braces — we're
        # assigning a fresh dict, but the JSON column's adapter
        # sometimes optimizes equal-value assignments away.
        flag_modified(user, "notification_preferences")
        db.commit()
        return dict(preferences)
