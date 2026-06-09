"""External user identity — hook + helpers.

Phase 2 of the role-based assignment system. Lets installs map
identifiers from a customer's source-of-truth user system (LMS,
HR, Auth0, etc.) to frontflow User rows. The Canvas SIS-ID model:
external system is the source of truth; frontflow keeps a
foreign-key reference plus whatever local state (assignments,
submissions) it owns.

Surface:

  - `User.external_id` column (already declared in store.py)
  - `@resolve_external_user` decorator for customer-side
    registration of the resolution hook
  - `resolve(external_id)` — look up an external_id, calling the
    registered hook on first touch and persisting the result;
    returns the User row or None
  - `lookup(external_id)` — non-resolving version: returns the
    existing User row by external_id, or None. Used when the
    caller knows the user should already exist (notification
    delivery, signed-link verification).

Default behavior with no hook registered: `resolve(external_id)`
returns None for unknown external_ids — the customer must
register a hook or pre-populate User rows manually. We do NOT
auto-create users from unverified strings.

Note: `store` imports are deferred (function-local) so importing
the dsl package doesn't trigger DB engine creation at load time
— the env-file resolution path depends on store staying lazy.
"""
from __future__ import annotations

from typing import Any, Callable, Optional


# The resolver hook. None until registered. Customer registers via
# the `@resolve_external_user` decorator at import time.
_resolver: Optional[Callable[[str], Optional[Any]]] = None


def resolve_external_user(
    fn: Callable[[str], Optional[Any]],
) -> Callable[[str], Optional[Any]]:
    """Register the function that resolves an external_id into a
    frontflow `User`. Called once per (request, unknown external_id)
    when frontflow encounters an id it doesn't have a row for.

    The hook is responsible for validation — it's the customer's
    chance to verify the external_id against their source system
    before frontflow creates a row.

    Usage:

        @frontflow.resolve_external_user
        def resolve(external_id: str) -> User | None:
            record = my_lms.find_user(external_id)
            if record is None:
                return None
            return User(
                username=record.username,
                external_id=external_id,
                is_admin=False,
            )

    Multiple registrations replace the prior hook — last wins.
    """
    global _resolver
    _resolver = fn
    return fn


def resolve(external_id: str) -> Optional[Any]:
    """Resolve an external identifier to a `User` row.

    Lookup order:
      1. Existing User with `external_id = external_id` → return it
         directly (no hook call; cheap path).
      2. No row found AND a resolver hook is registered → call the
         hook. If it returns a User, persist it and return it. If
         it returns None, the external_id is not valid; return None.
      3. No row found AND no hook registered → return None.

    The caller is responsible for handling None (typically: refuse
    the action, do not silently invent identities).
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session as DBSession
    from .store import User, _engine, _utcnow

    with DBSession(_engine) as session:
        existing = session.execute(
            select(User).where(User.external_id == external_id)
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        if _resolver is None:
            return None

        # First touch — defer to the customer hook.
        result = _resolver(external_id)
        if result is None:
            return None

        # The hook returned a User. Persist it. The hook is expected
        # to set external_id correctly; verify and overwrite if not,
        # since a mismatch would silently misroute future lookups.
        if result.external_id != external_id:
            result.external_id = external_id
        if result.created_at is None:
            result.created_at = _utcnow()
        session.add(result)
        session.commit()
        session.refresh(result)
        return result


def lookup(external_id: str) -> Optional[Any]:
    """Non-resolving version of `resolve`. Returns the existing
    User row by external_id, or None. Does NOT call the resolver
    hook. Used in paths where the user should already exist (e.g.,
    signed-link verification, where calling the hook on every
    request would be wasteful)."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session as DBSession
    from .store import User, _engine

    with DBSession(_engine) as session:
        return session.execute(
            select(User).where(User.external_id == external_id)
        ).scalar_one_or_none()


def deactivate(external_id: str, *, by_user_id: Optional[int] = None) -> bool:
    """Mark the external user as inactive. Preserves the row (and
    its audit history) but blocks future authentication and
    revokes any active assignments (Phase 4 — assignments table
    not yet present; this is a forward-compatible hook).

    Returns True if a row was found and deactivated; False if no
    user has that external_id.

    `by_user_id` records who initiated the deactivation; surfaced
    in audit when Phase 4 lands. For now, accepted but only
    logged.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session as DBSession
    from .store import User, _engine

    with DBSession(_engine) as session:
        user = session.execute(
            select(User).where(User.external_id == external_id)
        ).scalar_one_or_none()
        if user is None:
            return False
        user.is_active = False
        # Phase 4 hook: revoke all active submission_assignment rows
        # for this user. Table doesn't exist yet; the revocation
        # path lands with Phase 4. For now, marking the user
        # inactive is sufficient — the existing auth flow refuses
        # auth for inactive users.
        session.commit()
        return True


def update(
    external_id: str,
    *,
    username: Optional[str] = None,
    email: Optional[str] = None,
) -> Optional[Any]:
    """Update mapped attributes on an external user. external_id
    itself is NOT editable here — changing the external mapping
    requires deactivating the old row and creating a new one (the
    safer pattern, since assignments and submissions still
    reference the old user_id).

    Returns the updated User, or None if no row matches.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session as DBSession
    from .store import User, _engine

    with DBSession(_engine) as session:
        user = session.execute(
            select(User).where(User.external_id == external_id)
        ).scalar_one_or_none()
        if user is None:
            return None
        if username is not None:
            user.username = username
        if email is not None:
            # User model has no email column today; once added (or
            # if surfaced via a future profile attribute), update
            # here. Accepted for forward compatibility.
            setattr(user, "email", email)
        session.commit()
        session.refresh(user)
        return user


def _reset_for_tests() -> None:
    """Clear the registered resolver hook. Test-only helper —
    tests register a hook per fixture and must clear it after
    themselves to avoid leaking into other tests."""
    global _resolver
    _resolver = None
