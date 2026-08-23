"""Provisioning a person's Superset account for governed Explore.

Embedded dashboards need no account: frontflow mints a guest token and
puts the row filter on it. Explore is different — a guest token cannot
reach it (`GuestTokenResourceType` covers dashboards only), so Explore
runs under the viewer's *own* Superset session. That means an account,
a tier, and their row filter expressed as a Superset RLS rule rather
than as a token claim.

**This fails closed, unlike `provisioning`.** That module degrades on
purpose: a form should still render when the BI tool is down. Here the
question is whether someone may query data, and the only safe answer to
"I could not tell" is no.

**What this does not do: log anyone in.** frontflow can create the
account and assign the tier, but there is no single-sign-on bridge, so
the person still authenticates to Superset themselves. A freshly
created account has a random password nobody holds — deliberately, so
provisioning never mints a usable credential as a side effect — and an
administrator sets one, or SSO is configured. `ensure_account` reports
this back so the UI can say so rather than presenting a dead frame or a
bare login screen.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any, Optional

from . import rls, tiers
from .client import SupersetClient, SupersetError, SupersetUnreachable

logger = logging.getLogger(__name__)

# Datasets a governed account may explore. Kept in step with
# EXPLORABLE_DATASETS in deploy/superset/bootstrap_roles.py — the role
# grants access to them, and the rules below constrain which rows.
GOVERNED_DATASETS = ["v_frontflow_submissions"]


class AccountRefused(Exception):
    """This person may not have a governed Superset account."""


def _rule_name(username: str, dashboard: str) -> str:
    """One rule per (person, dashboard resolver).

    Superset ANDs a subject's rules together across rules, so several
    resolvers narrow rather than widen. That is the safe direction: a
    person governed by two dashboards sees the intersection.
    """
    return f"frontflow:{username}:{dashboard}"


def ensure_account(
    user: Any,
    *,
    dashboards: list[str],
    connection_name: Optional[str] = None,
) -> dict[str, Any]:
    """Make sure `user` has a Superset account matching their tier.

    `dashboards` are the governed dashboards whose row filters should
    constrain this person's Explore session — normally the ones the
    workspace declares.

    Raises `AccountRefused` when the user has no tier. Returns a summary
    describing what exists now, including whether the account is new and
    therefore not yet usable by the person themselves.
    """
    if user is None:
        raise AccountRefused("Not signed in.")

    role_name = tiers.role_for(user)
    if role_name is None:
        if not tiers.is_configured():
            raise AccountRefused(
                "This install does not offer Explore. Declare a tier with "
                'superset.tier("<group>", superset.EXPLORER) to enable it.'
            )
        raise AccountRefused(
            "You are not in a group that has been granted a Superset tier."
        )

    username = getattr(user, "username", None)
    if not username:
        raise AccountRefused("User has no username to mirror.")

    from ..dsl.connections import SupersetConnection

    connection_name = connection_name or SupersetConnection.DEFAULT_NAME

    try:
        with SupersetClient(connection_name) as client:
            role_id = client.find_role_id(role_name)
            if role_id is None:
                # The tier was declared but never defined in Superset. A
                # role frontflow created here would carry no permissions,
                # so refusing is more honest than provisioning a user
                # into an empty role and letting them wonder why
                # everything is missing.
                raise AccountRefused(
                    f"Superset has no role named {role_name!r}. An "
                    f"administrator needs to run "
                    f"deploy/superset/bootstrap_roles.py."
                )

            existed = client.find_user(username) is not None
            user_id = client.ensure_superset_user(
                username,
                email=getattr(user, "email", None) or f"{username}@frontflow.local",
                role_ids=[role_id],
                # Never returned, never logged, never stored. The account
                # is created so the tier and the rules have something to
                # attach to; making it usable is a separate, deliberate
                # act.
                password=secrets.token_urlsafe(32),
            )
            if user_id is None:
                raise AccountRefused(
                    "Could not create or find the Superset account."
                )

            subject_id = client.find_subject_for_user(user_id)
            if subject_id is None:
                raise AccountRefused(
                    "Superset has no Subject for this account, so no row "
                    "filter could be attached to it."
                )

            table_ids = []
            for table_name in GOVERNED_DATASETS:
                dataset_id = client.find_dataset_id(table_name)
                if dataset_id is not None:
                    table_ids.append(dataset_id)

            rules = []
            for dashboard in dashboards:
                if not rls.is_governed(dashboard):
                    continue
                # clause_for fails closed on its own: a resolver that
                # raises yields DENY_ALL rather than None.
                clause = rls.clause_for(dashboard, user)
                if clause is None or not table_ids:
                    continue
                client.ensure_rls_rule(
                    _rule_name(username, dashboard),
                    clause=clause,
                    table_ids=table_ids,
                    subject_ids=[subject_id],
                )
                rules.append({"dashboard": dashboard, "clause": clause})

    except AccountRefused:
        raise
    except (SupersetError, SupersetUnreachable) as exc:
        logger.warning("Could not provision Superset account: %s", exc)
        raise AccountRefused(f"Superset is not reachable: {exc}") from exc

    if not rules:
        # An ungoverned Explore session would see every row the role can
        # reach. Whether that is acceptable is the author's call, so say
        # it plainly rather than deciding silently.
        logger.info(
            "Explore for %r is not row-filtered: none of %r declares a "
            "row_filter.",
            username,
            dashboards,
        )

    return {
        "username": username,
        "tier": role_name,
        "account_created": not existed,
        "needs_password": not existed,
        "rules": rules,
        "governed": bool(rules),
    }
