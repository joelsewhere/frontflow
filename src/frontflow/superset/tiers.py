"""Which Superset account a person gets — declared by group.

    superset.tier("analysts", superset.ANALYST)
    superset.tier("*", superset.EXPLORER)

Row filters answer *which rows* a person may see. This answers a
different question: *how much query language* they may write to reach
them. The two are independent, and both apply — an analyst with SQL Lab
is still confined to their slice, because row-level security is enforced
in the query rather than in the UI.

**Why this is declared in the DSL, next to the row filters.** Capability
is an access-control decision, and access-control decisions belong in
version control where they can be reviewed, diffed, and explained. The
alternative — a column set through an admin screen — puts the most
security-relevant setting in the system in the one place nobody reviews.

**Groups, not users.** Groups are already how frontflow grants access to
folders, so "who is an analyst" is answered the same way as "who may see
the finance forms". A per-user override would need its own audit story.

**The tiers themselves are defined in Superset**, by an administrator
running `deploy/superset/bootstrap_roles.py`. This module only decides
who is assigned to one. Superset exposes no endpoint for setting a
role's permissions, so a role frontflow invented would carry none — see
`client.find_role_id`.

**Precedence.** A person in several groups gets the highest tier any of
them confers, matching how folder grants resolve (`auth._higher_role`).
The ranking is explicit below rather than implied by import order,
because two declarations disagreeing about someone's capability should
not be settled by which file was read first.

**Fail closed.** A user no declaration covers gets no tier, and so no
Superset account and no Explore. Silence means "not permitted", never
"permitted by default".
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("frontflow.superset.tiers")

# The roles bootstrap_roles.py defines. Referencing them by constant
# means a typo is an ImportError at startup rather than a person who
# silently never gets provisioned.
EXPLORER = "FrontFlow Explorer"
ANALYST = "FrontFlow Analyst"

# Higher value supersedes lower, exactly as auth._ROLE_RANK does for
# folder grants. Adding a tier means adding it here — an unranked tier
# raises rather than sorting arbitrarily.
TIER_RANK: dict[str, int] = {EXPLORER: 1, ANALYST: 2}

# The group name meaning "everyone who has no more specific tier".
DEFAULT_GROUP = "*"

# group name -> Superset role. Populated at import time by tier().
TIERS: dict[str, str] = {}


def tier(group: str, role: str) -> None:
    """Declare the Superset tier for members of a frontflow group.

    `group` is a frontflow group name, or `"*"` for a default applying
    to everyone otherwise uncovered.
    """
    key = (group or "").strip()
    if not key:
        raise ValueError(
            'superset.tier needs a group name, e.g. '
            'superset.tier("analysts", superset.ANALYST)'
        )

    if role not in TIER_RANK:
        known = ", ".join(sorted(TIER_RANK))
        raise ValueError(
            f"Unknown Superset tier {role!r} for group {key!r}. "
            f"Known tiers: {known}. Tiers are defined in Superset by "
            f"deploy/superset/bootstrap_roles.py; a name frontflow "
            f"invents would carry no permissions."
        )

    existing = TIERS.get(key)
    if existing is not None and existing != role:
        raise ValueError(
            f"Group {key!r} is already declared as {existing!r}. One tier "
            f"per group — two declarations disagreeing about a group's "
            f"capability should not be settled by import order."
        )
    TIERS[key] = role


def is_configured() -> bool:
    """Whether any tier has been declared at all.

    Distinguishes "this install does not use governed Explore" from
    "this person is not permitted", so the caller can say which.
    """
    return bool(TIERS)


def role_for_groups(group_names: list[str]) -> Optional[str]:
    """The highest tier these groups confer, or None if none do."""
    best: Optional[str] = None
    for name in group_names:
        role = TIERS.get(name)
        if role is None:
            continue
        if best is None or TIER_RANK[role] > TIER_RANK[best]:
            best = role

    if best is None:
        best = TIERS.get(DEFAULT_GROUP)
    return best


def role_for(user: Any) -> Optional[str]:
    """The Superset role `user` should hold, or None if not permitted.

    None means no account is provisioned and Explore is refused. That is
    the correct answer for a user nobody declared a tier for.
    """
    if user is None:
        return None

    from ..dsl import auth

    try:
        groups = auth.user_group_names(user)
    except Exception:  # noqa: BLE001 — a lookup failure must not grant access
        logger.exception(
            "Could not resolve groups for user %r; refusing a Superset tier.",
            getattr(user, "username", user),
        )
        return None

    return role_for_groups(groups)
