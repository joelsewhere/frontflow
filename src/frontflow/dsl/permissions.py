"""Role-based permission resolution.

Phase 1 of the role-based assignment system. Computes whether a
user is permitted to read or write a given node / field on a
given submission, based on:

  - The form's permission template (declared in DSL, snapshotted
    in the form_version row).
  - The user's role assignments on the submission (rows in
    submission_assignment — Phase 4; absent for now).
  - The user's admin status (admins always permitted).

For Phase 1 specifically (no Assign operator yet, no
submission_assignment table), the rule is simpler:

  - default_role_mode == "open" AND no per-node/per-input role
    declared → fully permitted (matches today's behavior).
  - default_role_mode == "open" but node/input has role= → only
    explicitly granted users (assignments table) can act. Since
    Phase 1 has no assignments table, that effectively means only
    admins can act on role-gated nodes until Phase 4 ships.
  - default_role_mode == "strict" → every node has role=;
    same logic.

The auth check is a value-typed `NodeAccess` returned per node,
plus per-field `FieldAccess`. Callers consume these to decide
between "render normally" / "render pending" / "block submit".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class FieldAccess:
    """Whether a user can read or write one specific field on a
    node. `pending=True` means the user can see the field's label
    but the input is disabled (the field is gated on a role the
    user doesn't hold). `read` is always implied by `write`."""
    can_read: bool
    can_write: bool
    pending: bool = False


@dataclass(frozen=True)
class NodeAccess:
    """Whether a user can read or write a node. `pending=True`
    means the node would otherwise be reachable but is gated on a
    role the user doesn't hold; UI renders pending state. Distinct
    from no-access (no read), which renders nothing."""
    can_read: bool
    can_write: bool
    pending: bool = False
    # The role identifiers the user IS missing for write access.
    # Populated when `pending=True` so the UI can render
    # "this is assigned to <role>". Empty when not pending.
    missing_write_roles: tuple[str, ...] = ()


def resolve_node_access(
    node_role: Optional[dict[str, list[str]]],
    default_role_mode: str,
    *,
    user_roles_on_submission: frozenset[str],
    is_admin: bool,
) -> NodeAccess:
    """Decide a user's access to a single node.

    Args:
      node_role: the CompiledNode.role dict, or None if the node
        has no role= declaration.
      default_role_mode: "open" (no role=, anyone with form
        access) or "strict" (explicit role= required).
      user_roles_on_submission: identifiers of every role the
        user is currently assigned on this submission.
      is_admin: admin users always have full access.
    """
    if is_admin:
        return NodeAccess(can_read=True, can_write=True)

    if node_role is None:
        # No per-node gate. In "open" mode, anyone with form-level
        # access reaches this node. In "strict" mode, this would be
        # a compile-time error — defensive `False` here in case the
        # snapshot is stale.
        if default_role_mode == "open":
            return NodeAccess(can_read=True, can_write=True)
        return NodeAccess(can_read=False, can_write=False)

    write_roles = set(node_role.get("write", []))
    read_roles = set(node_role.get("read", []))
    # Anyone in write is auto-in-read; the snapshot already merges
    # these, but guard against an older snapshot.
    read_roles |= write_roles

    can_write = bool(write_roles & user_roles_on_submission)
    can_read = bool(read_roles & user_roles_on_submission) or can_write

    if can_read and not can_write:
        # Read access only — the user can see the node's state, but
        # the inputs are disabled. Not "pending" — pending is when
        # the user has NO access at all to the node but the node
        # would otherwise be reachable.
        return NodeAccess(can_read=True, can_write=False)

    if not can_read:
        # No access; surface as pending so the UI can render
        # "assigned to <role>" rather than a hard 404.
        return NodeAccess(
            can_read=False,
            can_write=False,
            pending=True,
            missing_write_roles=tuple(sorted(write_roles)),
        )

    return NodeAccess(can_read=can_read, can_write=can_write)


def resolve_field_access(
    field_role: Optional[str],
    node_access: NodeAccess,
    *,
    user_roles_on_submission: frozenset[str],
    is_admin: bool,
) -> FieldAccess:
    """Decide a user's access to a single field within a node.

    Per-input role= narrows write access for that specific field;
    read still follows the node's read permission (see design
    doc §1.1 — per-input read was explicitly dropped).
    """
    if is_admin:
        return FieldAccess(can_read=True, can_write=True)

    if not node_access.can_read:
        # No node read → no field access.
        return FieldAccess(can_read=False, can_write=False)

    if field_role is None:
        # No per-input gate → field write follows node write.
        return FieldAccess(
            can_read=True,
            can_write=node_access.can_write,
        )

    # Per-input role narrows write: user must have BOTH node write
    # AND the specific input's role.
    has_field_role = field_role in user_roles_on_submission
    can_write = node_access.can_write and has_field_role
    # If the user has node write but not the field's role, the
    # field renders pending (label visible, input disabled).
    pending = node_access.can_write and not has_field_role
    return FieldAccess(
        can_read=True,
        can_write=can_write,
        pending=pending,
    )
