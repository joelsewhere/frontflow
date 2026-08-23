"""Row-level security — which rows of a dashboard a person may see.

    @superset.row_filter("sales_overview")
    def scope(user):
        regions = lookup_regions(user.external_id)
        return f"region IN ({sql_list(regions)})"

The function returns a SQL predicate. frontflow passes it as `rls` on
the guest token it mints, so Superset applies it inside the query — the
viewer cannot widen it by clicking, because it is not a filter they
hold. Filtering and access control are separate concerns: a person may
still filter freely within their slice.

**This fails closed.** A resolver that raises, or returns something that
is not a usable clause, denies everything rather than falling back to
unrestricted. The opposite default — no clause means no restriction — is
the worst possible behaviour for an access-control mechanism, because it
turns any bug into a silent data leak. A denied dashboard renders empty
and the failure is logged; that is loud, recoverable, and safe.

A dashboard with no resolver is unrestricted, which is the right default
for the dashboards that already exist.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger("frontflow.superset.rls")

# A predicate that matches nothing. Used when a resolver fails: the
# viewer sees an empty dashboard rather than someone else's data.
DENY_ALL = "1 = 0"

# dashboard name -> resolver. Populated by the decorator at import time,
# the same way @workspace.navigation and @form register.
RESOLVERS: dict[str, Callable[[Any], Optional[str]]] = {}


def row_filter(name: str) -> Callable:
    """Declare the row filter for a dashboard.

    One resolver per dashboard. A second declaration is an author error
    rather than a silent replacement — two functions disagreeing about
    who may see what is exactly the kind of thing that should not be
    settled by import order.
    """
    key = (name or "").strip()
    if not key:
        raise ValueError(
            'superset.row_filter needs a dashboard name, e.g. '
            '@superset.row_filter("sales_overview")'
        )

    def wrap(fn: Callable[[Any], Optional[str]]) -> Callable:
        existing = RESOLVERS.get(key)
        if existing is not None and existing is not fn:
            raise ValueError(
                f"Dashboard {key!r} already has a row filter "
                f"({existing.__name__!r}). One per dashboard — two "
                f"functions disagreeing about who may see what should "
                f"not be settled by import order."
            )
        RESOLVERS[key] = fn
        return fn

    return wrap


def is_governed(name: str) -> bool:
    """Whether this dashboard has a row filter at all."""
    return (name or "").strip() in RESOLVERS


def clause_for(name: str, user: Any) -> Optional[str]:
    """The SQL predicate limiting `user` on dashboard `name`.

    Returns None when the dashboard is unrestricted — no resolver — and
    `DENY_ALL` when one exists but could not produce a clause.
    """
    resolver = RESOLVERS.get((name or "").strip())
    if resolver is None:
        return None

    try:
        clause = resolver(user)
    except Exception:  # noqa: BLE001 — a raising resolver must not open the door
        logger.exception(
            "Row filter for dashboard %r raised; denying all rows.", name
        )
        return DENY_ALL

    if clause is None:
        # Explicitly "no rows for this person" — distinct from a
        # dashboard having no resolver. A resolver that means
        # "unrestricted" has to say so by not existing.
        logger.warning(
            "Row filter for dashboard %r returned None; denying all rows.",
            name,
        )
        return DENY_ALL

    if not isinstance(clause, str) or not clause.strip():
        logger.error(
            "Row filter for dashboard %r returned %r, which is not a SQL "
            "clause; denying all rows.",
            name,
            clause,
        )
        return DENY_ALL

    return clause.strip()


def rules_for(name: str, user: Any) -> list[dict[str, str]]:
    """The `rls` payload for a guest token — what Superset wants."""
    clause = clause_for(name, user)
    if clause is None:
        return []
    return [{"clause": clause}]
