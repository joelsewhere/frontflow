"""Create the read-only Explorer role. Run once, inside Superset.

    docker compose exec -T superset python < superset/bootstrap_explorer_role.py

**Why this is not done by frontflow.** Superset exposes no HTTP endpoint
for setting a role's permissions — `/api/v1/security/roles/` accepts a
name and nothing else — so a role frontflow created over the API would
carry no permissions at all. Defining it is an administrator's job,
inside Superset. frontflow then assigns people to it and manages their
row-level security rules, both of which the API does support.

**What the role is for.** "Build your own visualizations, but do not
save them." Explore runs under the viewer's own Superset account, so
this is the account it runs under. Gamma is the closest built-in and is
NOT sufficient on its own: it holds `can_write` on both Chart and
Dashboard, so a Gamma user can save charts and edit dashboards. This
clones Gamma and removes exactly the permissions that let a person
persist something.

Saving is refused server-side, so the fact that a Save button may still
render is cosmetic — the request fails.
"""

from superset.app import create_app

ROLE_NAME = "FrontFlow Explorer"

# Datasets this role may explore. Gamma grants no datasource access at
# all, so without this the role is secure and useless: a person can log
# in, and see nothing. Naming the datasets here — rather than granting
# the whole database — keeps "what may be explored" an explicit
# decision, next to "what may be saved".
EXPLORABLE_DATASETS = ["v_frontflow_submissions"]

# Permissions that let a person PERSIST something. Everything else Gamma
# holds — reading charts and dashboards, running queries, exploring — is
# the point of the role and is kept.
WITHHELD = {
    ("can_write", "Chart"),
    ("can_write", "Dashboard"),
    # Tagging and chart customisations are writes too: they change what
    # other people see, which is the line this role is drawn along.
    ("can_tag", "Chart"),
    ("can_tag", "Dashboard"),
    ("can_put_chart_customizations", "Dashboard"),
    # Embedding is frontflow's to configure, not a viewer's to remove.
    ("can_delete_embedded", "Dashboard"),
}


def main() -> None:
    app = create_app()
    with app.app_context():
        from superset import security_manager as sm

        gamma = sm.find_role("Gamma")
        if gamma is None:
            raise SystemExit("No Gamma role to clone — is this Superset set up?")

        role = sm.find_role(ROLE_NAME) or sm.add_role(ROLE_NAME)

        kept, dropped = [], []
        for permission in gamma.permissions:
            pair = (
                permission.permission.name,
                permission.view_menu.name,
            )
            (dropped if pair in WITHHELD else kept).append(pair)
            if pair not in WITHHELD:
                sm.add_permission_role(role, permission)

        # Re-run safe: strip anything withheld that a previous run — or a
        # person — granted. A role that only ever gains permissions would
        # drift open, which is the wrong direction for this one.
        for permission in list(role.permissions):
            pair = (permission.permission.name, permission.view_menu.name)
            if pair in WITHHELD:
                sm.del_permission_role(role, permission)

        # Datasource access, per named dataset. Row-level security
        # then narrows WHICH ROWS each person sees within them; this is
        # only the coarser question of which tables exist for them.
        from superset import db
        from superset.connectors.sqla.models import SqlaTable

        granted = []
        for table_name in EXPLORABLE_DATASETS:
            dataset = (
                db.session.query(SqlaTable)
                .filter_by(table_name=table_name)
                .one_or_none()
            )
            if dataset is None:
                print(f"  no dataset named {table_name!r} — skipped")
                continue
            permission = sm.find_permission_view_menu(
                "datasource_access", dataset.perm
            )
            if permission is None:
                permission = sm.add_permission_view_menu(
                    "datasource_access", dataset.perm
                )
            sm.add_permission_role(role, permission)
            granted.append(dataset.perm)

        db.session.commit()

        print(f"{ROLE_NAME}: {len(kept)} permissions kept")
        for perm in granted:
            print(f"  explorable: {perm}")
        for pair in sorted(dropped):
            print(f"  withheld: {pair[0]} on {pair[1]}")


if __name__ == "__main__":
    main()
