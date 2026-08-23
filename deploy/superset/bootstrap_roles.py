"""Create frontflow's Superset roles. Run once, inside Superset.

    docker compose exec -T superset python < superset/bootstrap_roles.py

**Why this is not done by frontflow.** Superset exposes no HTTP endpoint
for setting a role's permissions — `/api/v1/security/roles/` accepts a
name and nothing else — so a role frontflow created over the API would
carry no permissions at all. Defining them is an administrator's job,
inside Superset. frontflow then assigns people to a role and manages
their row-level security rules, both of which the API does support.

**The tiers.** Two kinds of person use the same data and need different
things from it:

    FrontFlow Explorer   read dashboards, build your own charts ad hoc,
                         save nothing
    FrontFlow Analyst    the same, plus real querying power — SQL Lab,
                         sub-queries in adhoc SQL, Jinja in SQL

Both are read-only, and both are subject to row-level security. The only
axis between them is how much query language they may write. That is
deliberate: "can this person persist something others will see" and "how
expressive may their queries be" are separate questions, and collapsing
them is what makes BI permissions unmanageable.

**What makes the Analyst tier safe to hand out.** Three things have to
hold together, and none of them is sufficient alone:

1. The database grant. `superset_ro` may select from the reporting views
   and nothing else (see `deploy/postgres/01-init.sh` and
   `store._grant_reporting_view_to_superset_ro`). This is the real
   boundary — everything below is enforcement *within* it.
2. `RLS_IN_SQLLAB`, on globally. Superset resolves RLS predicates per
   dataset, so a query naming a table with no dataset gets no predicate.
   Point 1 is what guarantees no such table is reachable.
3. Neither role may create datasets, so neither can register a new table
   to explore around the rules.

Saving is refused server-side, so a Save button that still renders is
cosmetic — the request fails.

Query-language capability itself is NOT granted here. It is resolved
per-request from the caller's roles by `IS_FEATURE_ENABLED_FUNC` in
`superset_config.py`, which is where PER_ROLE_FEATURES lists which roles
get `ALLOW_ADHOC_SUBQUERY` and `ENABLE_TEMPLATE_PROCESSING`. Keep the
role names here and there in step.
"""

from superset.app import create_app

EXPLORER_ROLE = "FrontFlow Explorer"
ANALYST_ROLE = "FrontFlow Analyst"

# Datasets these roles may explore. Gamma grants no datasource access at
# all, so without this a role is secure and useless: a person can log in,
# and see nothing. Naming the datasets here — rather than granting the
# whole database — keeps "what may be explored" an explicit decision,
# next to "what may be saved".
EXPLORABLE_DATASETS = ["v_frontflow_submissions"]

# Permissions that let a person PERSIST something. Everything else Gamma
# holds — reading charts and dashboards, running queries, exploring — is
# the point of these roles and is kept. Withheld from BOTH tiers.
WITHHELD = {
    ("can_write", "Chart"),
    ("can_write", "Dashboard"),
    # Tagging and chart customisations are writes too: they change what
    # other people see, which is the line these roles are drawn along.
    ("can_tag", "Chart"),
    ("can_tag", "Dashboard"),
    ("can_put_chart_customizations", "Dashboard"),
    # Embedding is frontflow's to configure, not a viewer's to remove.
    ("can_delete_embedded", "Dashboard"),
    # Creating a dataset would let someone register a table and explore
    # around the rules. The grant in point 1 above already stops them
    # reading anything new, but refusing this keeps the failure legible
    # rather than a confusing permission error later.
    ("can_write", "Dataset"),
}

# Granted to the Analyst tier only: the SQL Lab surface. Safe only
# because of the three conditions in the module docstring.
ANALYST_EXTRA = [
    ("menu_access", "SQL Lab"),
    ("menu_access", "SQL Editor"),
    ("can_sqllab", "Superset"),
    ("can_read", "SavedQuery"),
    ("can_write", "SavedQuery"),
    ("can_execute_sql_query", "SQLLab"),
    ("can_estimate_query_cost", "SQLLab"),
    ("can_export_csv", "SQLLab"),
    ("can_read", "SQLLab"),
]


def _sync_role(sm, db, name, extra_pairs):
    """Create or update one role: Gamma, minus WITHHELD, plus extra_pairs."""
    gamma = sm.find_role("Gamma")
    if gamma is None:
        raise SystemExit("No Gamma role to clone — is this Superset set up?")

    role = sm.find_role(name) or sm.add_role(name)

    kept = []
    for permission in gamma.permissions:
        pair = (permission.permission.name, permission.view_menu.name)
        if pair in WITHHELD:
            continue
        kept.append(pair)
        sm.add_permission_role(role, permission)

    # The extra capability surface for this tier.
    added_extra = []
    for perm_name, view_name in extra_pairs:
        permission = sm.find_permission_view_menu(perm_name, view_name)
        if permission is None:
            # A view menu that does not exist on this build. Report it
            # rather than inventing it — a silently-missing SQL Lab
            # permission would leave the tier quietly inert.
            print(f"  ! no such permission: {perm_name} on {view_name} — skipped")
            continue
        sm.add_permission_role(role, permission)
        added_extra.append((perm_name, view_name))

    # Re-run safe: strip anything withheld that a previous run — or a
    # person — granted. A role that only ever gains permissions would
    # drift open, which is the wrong direction for these.
    allowed_extra = set(extra_pairs)
    removed = []
    for permission in list(role.permissions):
        pair = (permission.permission.name, permission.view_menu.name)
        if pair in WITHHELD and pair not in allowed_extra:
            sm.del_permission_role(role, permission)
            removed.append(pair)

    # Datasource access, per named dataset. Row-level security then
    # narrows WHICH ROWS each person sees within them; this is only the
    # coarser question of which tables exist for them.
    from superset.connectors.sqla.models import SqlaTable

    granted = []
    for table_name in EXPLORABLE_DATASETS:
        dataset = (
            db.session.query(SqlaTable).filter_by(table_name=table_name).one_or_none()
        )
        if dataset is None:
            print(f"  ! no dataset named {table_name!r} — skipped")
            continue
        permission = sm.find_permission_view_menu("datasource_access", dataset.perm)
        if permission is None:
            permission = sm.add_permission_view_menu("datasource_access", dataset.perm)
        sm.add_permission_role(role, permission)
        granted.append(dataset.perm)

    return role, kept, added_extra, removed, granted


def main() -> None:
    app = create_app()
    with app.app_context():
        from superset import db
        from superset import security_manager as sm

        for name, extra in ((EXPLORER_ROLE, []), (ANALYST_ROLE, ANALYST_EXTRA)):
            role, kept, added, removed, granted = _sync_role(sm, db, name, extra)
            db.session.commit()

            print(f"\n{name}: {len(kept)} base permissions")
            for perm in granted:
                print(f"  explorable: {perm}")
            for pair in added:
                print(f"  granted:    {pair[0]} on {pair[1]}")
            for pair in sorted(removed):
                print(f"  revoked:    {pair[0]} on {pair[1]}")
            for pair in sorted(WITHHELD):
                print(f"  withheld:   {pair[0]} on {pair[1]}")


if __name__ == "__main__":
    main()
