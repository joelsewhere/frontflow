"""Tests for `@workspace` — forms and dashboards on one screen.

The workspace is what authorizes its dashboard panels. A dashboard
inside a form borrows that form's ACL; a dashboard in a workspace has
none to borrow, so the workspace's own visibility decides. That makes
`TestAccessControl` the load-bearing part of this file.

A second rule matters and is easy to get wrong in the permissive
direction: a workspace grants access to its *dashboards*, never to the
*forms* it contains. A restricted form placed in a public workspace must
stay restricted.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from frontflow.dsl import auth, store
from frontflow.dsl.workspaces import (
    WORKSPACES,
    Form,
    compile_workspace,
    workspace as workspace_decorator,
)
from frontflow import displays


PUBLIC = "ops_public"
PRIVATE = "ops_private"


class TestDeclaration:
    def test_body_must_return_a_panel_tree(self):
        """A workspace that returns nothing is an author error, caught at
        registration rather than rendering as a blank screen."""

        @workspace_decorator(workspace_id="ws_nothing")
        def nothing():
            return None

        with pytest.raises(ValueError, match="returned nothing"):
            nothing()
        WORKSPACES.pop("ws_nothing", None)

    def test_a_workspace_needs_at_least_one_panel(self):
        @workspace_decorator(workspace_id="ws_empty")
        def empty():
            return displays.Column()

        with pytest.raises(ValueError, match="no panels"):
            empty()
        WORKSPACES.pop("ws_empty", None)

    def test_form_panel_requires_an_id(self):
        with pytest.raises(ValueError):
            Form("")

    def test_compiles_to_the_same_block_shape_forms_use(self):
        """One tree format, so the browser's recursive renderer serves
        both surfaces."""

        @workspace_decorator(workspace_id="ws_shape", title="Shape")
        def shape():
            return displays.Row(
                Form("ws_entry"), displays.Dashboard("d")
            )

        compiled = compile_workspace(shape())
        WORKSPACES.pop("ws_shape", None)

        layout = compiled["layout"]
        assert layout["type"] == "row"
        kinds = [c["type"] for c in layout["children"]]
        assert kinds == ["workspace_form", "dashboard"]
        assert layout["children"][0]["props"]["form_id"] == "ws_entry"
        assert layout["children"][1]["props"]["name"] == "d"


class TestDiscovery:
    def test_declared_workspaces_are_served(self, app):
        import frontflow.main as main_mod

        assert PUBLIC in main_mod.WORKSPACE_LAYOUTS
        assert PRIVATE in main_mod.WORKSPACE_LAYOUTS

    def test_dsl_private_sets_initial_visibility(self, app):
        assert store.get_workspace(PRIVATE)["visibility"] == "restricted"
        assert store.get_workspace(PUBLIC)["visibility"] == "public"

    def test_a_workspace_naming_an_unknown_form_is_a_load_error(self, app):
        """A typo should degrade that workspace, not the forms beside it,
        and not render as a silently blank panel.

        `_scan_workspaces` is driven directly because `scan_workflows`
        clears the registry first — a workspace registered here would not
        survive into the scan.
        """
        import frontflow.main as main_mod

        @workspace_decorator(workspace_id="ws_broken")
        def broken():
            return displays.Row(Form("no_such_form_anywhere"))

        broken()
        errors: dict[str, str] = {}
        try:
            main_mod._scan_workspaces(main_mod.FORMS, errors)

            assert "ws_broken" not in main_mod.WORKSPACE_LAYOUTS
            assert "ws_broken" in errors
            assert "unknown form" in errors["ws_broken"]
            # The valid workspaces beside it still load.
            assert PUBLIC in main_mod.WORKSPACE_LAYOUTS
        finally:
            WORKSPACES.pop("ws_broken", None)
            main_mod.scan_workflows()


class TestAccessControl:
    def test_public_workspace_is_readable_by_anyone(
        self, anon_client: TestClient
    ):
        assert anon_client.get(f"/api/workspaces/{PUBLIC}").status_code == 200

    def test_restricted_workspace_404s_for_anonymous(
        self, anon_client: TestClient
    ):
        """404 not 403 — a restricted workspace's existence is not leaked
        by probing, matching how forms behave."""
        r = anon_client.get(f"/api/workspaces/{PRIVATE}")
        assert r.status_code == 404, r.text

    def test_admin_reaches_a_restricted_workspace(
        self, admin_client: TestClient
    ):
        assert admin_client.get(f"/api/workspaces/{PRIVATE}").status_code == 200

    # NOTE: `admin_client` logs in ON the `anon_client` instance — they
    # are the same object. Requesting both in one test would make the
    # "anonymous" client authenticated, so these are separate tests.

    def test_listing_hides_what_an_anonymous_caller_cannot_open(
        self, anon_client: TestClient
    ):
        """A listing that 403s would itself disclose which workspaces
        exist, so the list is filtered instead."""
        ids = {w["workspace_id"] for w in anon_client.get("/api/workspaces").json()}

        assert PUBLIC in ids
        assert PRIVATE not in ids

    def test_listing_shows_an_admin_everything(self, admin_client: TestClient):
        ids = {w["workspace_id"] for w in admin_client.get("/api/workspaces").json()}

        assert PUBLIC in ids
        assert PRIVATE in ids


class TestDashboardAuthorization:
    """The reason workspace visibility exists."""

    def test_restricted_workspace_denies_its_dashboard(
        self, anon_client: TestClient
    ):
        r = anon_client.post(
            f"/api/workspaces/{PRIVATE}/dashboards/secret_ops_metrics/guest-token"
        )
        assert r.status_code == 404, r.text

    def test_cannot_name_a_dashboard_the_workspace_does_not_contain(
        self, admin_client: TestClient
    ):
        """Otherwise any workspace becomes a key to every dashboard."""
        r = admin_client.post(
            f"/api/workspaces/{PUBLIC}/dashboards/some_other_dashboard/guest-token"
        )
        assert r.status_code == 404, r.text


class TestWorkspaceDoesNotWidenFormAccess:
    def test_a_form_panel_still_obeys_the_forms_own_visibility(
        self, anon_client: TestClient, app
    ):
        """A workspace grants access to its dashboards, never to the forms
        it contains. Restricting the form must still hide it even though
        the workspace stays public."""
        auth.set_form_visibility("ws_entry", "restricted")
        try:
            # The workspace itself is still readable...
            assert anon_client.get(f"/api/workspaces/{PUBLIC}").status_code == 200
            # ...but the form it contains is not.
            assert anon_client.get("/api/forms/ws_entry").status_code == 404
        finally:
            auth.set_form_visibility("ws_entry", "public")


class TestDashboardEditGate:
    """`manage` decides whether frontflow OFFERS the Superset editor.

    Two things it deliberately does not do:

      * It confers no Superset rights. frontflow users are not Superset
        users, so what someone can actually save is decided by their own
        Superset login. This flag only controls whether the toggle
        appears.
      * It has nothing to do with editing FORMS. A form's definition
        lives in its DSL source and is never editable from the UI.
    """

    def test_admin_is_offered_the_editor(self, admin_client: TestClient):
        body = admin_client.get(f"/api/workspaces/{PUBLIC}").json()
        assert body["access"] == "manage"
        assert body["can_edit_dashboards"] is True

    def test_anonymous_viewer_is_not(self, anon_client: TestClient):
        body = anon_client.get(f"/api/workspaces/{PUBLIC}").json()
        assert body["access"] == "view"
        assert body["can_edit_dashboards"] is False

    def test_a_view_grant_does_not_offer_the_editor(
        self, user_client: TestClient, app
    ):
        """Reaching a restricted workspace is not the same as managing
        its dashboards."""
        user = auth.get_user_by_username("user")
        store.grant_workspace_access(PRIVATE, user.id, role="view")

        body = user_client.get(f"/api/workspaces/{PRIVATE}").json()
        assert body["access"] == "view"
        assert body["can_edit_dashboards"] is False

    def test_a_manage_grant_does(self, user_client: TestClient, app):
        user = auth.get_user_by_username("user")
        store.grant_workspace_access(PRIVATE, user.id, role="manage")

        body = user_client.get(f"/api/workspaces/{PRIVATE}").json()
        assert body["access"] == "manage"
        assert body["can_edit_dashboards"] is True

    def test_a_grant_is_scoped_to_its_workspace(
        self, user_client: TestClient, app
    ):
        """Managing one workspace must not confer anything on another."""
        user = auth.get_user_by_username("user")
        store.grant_workspace_access(PRIVATE, user.id, role="manage")

        other = user_client.get(f"/api/workspaces/{PUBLIC}").json()
        assert other["can_edit_dashboards"] is False

    def test_role_must_be_valid(self, app):
        with pytest.raises(ValueError):
            store.grant_workspace_access(PUBLIC, 1, role="superuser")


class TestExploreAndTabs:
    """Explore is a panel, not a mode — so it gets its own tab and can be
    dragged anywhere. `Tabs` expresses "these share one dock group"."""

    def test_explore_compiles_as_a_panel(self):
        from frontflow.dsl.workspaces import Explore

        @workspace_decorator(workspace_id="ws_explore")
        def ws():
            return displays.Row(
                Form("ws_entry"), Explore(dataset="v_frontflow_submissions")
            )

        compiled = compile_workspace(ws())
        WORKSPACES.pop("ws_explore", None)

        kinds = [c["type"] for c in compiled["layout"]["children"]]
        assert kinds == ["workspace_form", "superset_explore"]
        explore = compiled["layout"]["children"][1]
        assert explore["props"]["dataset"] == "v_frontflow_submissions"

    def test_explore_without_a_dataset_is_allowed(self):
        """No dataset means Superset's picker — the right default when a
        workspace has several things worth exploring."""
        from frontflow.dsl.workspaces import Explore

        @workspace_decorator(workspace_id="ws_explore_any")
        def ws():
            return displays.Column(Explore())

        compiled = compile_workspace(ws())
        WORKSPACES.pop("ws_explore_any", None)
        assert compiled["layout"]["children"][0]["props"]["dataset"] is None

    def test_tabs_container_nests_its_panels(self):
        from frontflow.dsl.workspaces import Explore, Tabs

        @workspace_decorator(workspace_id="ws_tabs")
        def ws():
            return displays.Row(
                Form("ws_entry"),
                Tabs(displays.Dashboard("d"), Explore()),
            )

        compiled = compile_workspace(ws())
        WORKSPACES.pop("ws_tabs", None)

        tabs = compiled["layout"]["children"][1]
        assert tabs["type"] == "tabs"
        assert [c["type"] for c in tabs["children"]] == [
            "dashboard",
            "superset_explore",
        ]

    def test_an_explore_only_workspace_has_panels(self):
        """Explore counts as a panel — a workspace of just Explore is
        valid, not 'no panels'."""
        from frontflow.dsl.workspaces import Explore

        @workspace_decorator(workspace_id="ws_only_explore")
        def ws():
            return displays.Column(Explore())

        ws()  # must not raise
        WORKSPACES.pop("ws_only_explore", None)


class TestNavigation:
    """`@workspace.navigation` — the side nav and top navbar.

    Navigation is deliberately not a vocabulary of its own. A nav is an
    ordinary dock panel holding ordinary display blocks, which is what
    makes anything true of a panel true of it: it collapses, it resizes,
    it re-docks. These tests pin the parts that would otherwise drift —
    that one declaration serves every workspace, that a workspace can
    still opt out, and that nothing which needs a submission can be
    smuggled into chrome that has none.
    """

    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        from frontflow.dsl.workspaces import DEFAULT_NAVIGATION

        saved = dict(DEFAULT_NAVIGATION)
        DEFAULT_NAVIGATION.clear()
        yield
        DEFAULT_NAVIGATION.clear()
        DEFAULT_NAVIGATION.update(saved)

    @staticmethod
    def _workspace(ws_id: str, **kwargs):
        @workspace_decorator(workspace_id=ws_id, **kwargs)
        def body():
            return displays.Row(Form("sales"))

        try:
            return body()
        finally:
            WORKSPACES.pop(ws_id, None)

    def test_one_declaration_serves_every_workspace(self):
        """The point of a default: declare navigation once, and every
        workspace has it without repeating itself."""
        from frontflow import workspace as ws_ns

        @ws_ns.navigation
        def main_nav():
            return ws_ns.Nav(displays.Markdown("- [Home](/)"))

        for ws_id in ("nav_a", "nav_b"):
            compiled = compile_workspace(self._workspace(ws_id))
            assert compiled["nav"] is not None
            assert compiled["nav"]["children"][0]["type"] == "markdown"

    def test_a_workspace_can_substitute_its_own(self):
        from frontflow import workspace as ws_ns

        @ws_ns.navigation
        def main_nav():
            return ws_ns.Nav(displays.Markdown("default"))

        own = ws_ns.Nav(displays.Markdown("bespoke"))
        compiled = compile_workspace(self._workspace("nav_own", nav=own))
        assert compiled["nav"]["children"][0]["props"]["source"] == "bespoke"

    def test_a_workspace_can_opt_out_entirely(self):
        """`nav=None` must be distinguishable from saying nothing at all,
        or a workspace could never refuse the default."""
        from frontflow import workspace as ws_ns

        @ws_ns.navigation
        def main_nav():
            return ws_ns.Nav(displays.Markdown("default"))

        assert compile_workspace(self._workspace("nav_off", nav=None))["nav"] is None
        assert compile_workspace(self._workspace("nav_on"))["nav"] is not None

    def test_nav_and_navbar_are_independent(self):
        from frontflow import workspace as ws_ns

        @ws_ns.navigation
        def side():
            return ws_ns.Nav(displays.Markdown("side"))

        @ws_ns.navigation
        def top():
            return ws_ns.Navbar(displays.Markdown("top"))

        compiled = compile_workspace(self._workspace("nav_both"))
        assert compiled["nav"]["position"] == "left"
        assert compiled["navbar"]["position"] == "top"

    def test_navigation_holds_any_display_block(self):
        """No nav-specific vocabulary: a block is the same block here as
        in a form, compiled by the same compiler."""
        from frontflow import workspace as ws_ns

        nav = ws_ns.Nav(
            displays.Markdown("# Ops"),
            displays.Divider(),
            displays.Image("/logo.svg"),
        )
        compiled = compile_workspace(self._workspace("nav_blocks", nav=nav))
        assert [c["type"] for c in compiled["nav"]["children"]] == [
            "markdown",
            "divider",
            "image",
        ]

    def test_inputs_in_navigation_are_an_author_error(self):
        """Chrome belongs to the workspace, not to a submission — there is
        nothing for a field to bind to, so this must fail loudly rather
        than render a field that can never be submitted."""
        from frontflow import inputs, workspace as ws_ns

        nav = ws_ns.Nav(inputs.Text(id="who", label="Who"))
        with pytest.raises(TypeError, match="display-only"):
            compile_workspace(self._workspace("nav_input", nav=nav))

    def test_a_nav_cannot_pin_to_the_top(self):
        """Nav and Navbar differ by the edge they pin to; mixing them up
        should name the fix rather than render sideways."""
        from frontflow import workspace as ws_ns

        with pytest.raises(ValueError, match="must be one of"):
            ws_ns.Nav(position="top")
        with pytest.raises(ValueError, match="must be one of"):
            ws_ns.Navbar(position="left")

    def test_passing_a_navbar_as_the_nav_is_caught(self):
        from frontflow import workspace as ws_ns

        with pytest.raises(TypeError, match="pass it as navbar"):
            self._workspace("nav_mixed", nav=ws_ns.Navbar())

    def test_navigation_opens_collapsed_by_default(self):
        """A workspace is about its panels. Navigation should not spend
        screen on itself until asked."""
        from frontflow import workspace as ws_ns

        assert ws_ns.Nav().collapsed is True
        assert ws_ns.Nav(collapsed=False).collapsed is False

    def test_the_handle_is_carried_through_to_the_client(self):
        """The spine is what people actually see of a nav, so its
        appearance has to survive compilation."""
        from frontflow import workspace as ws_ns

        nav = ws_ns.Nav(
            displays.Markdown("x"),
            handle=ws_ns.Handle(icon="☰", label="Menu", position="center"),
        )
        handle = compile_workspace(self._workspace("nav_handle", nav=nav))["nav"][
            "handle"
        ]
        assert handle == {"label": "Menu", "icon": "☰", "position": "center"}

    def test_only_one_default_per_panel_kind(self):
        from frontflow import workspace as ws_ns

        @ws_ns.navigation
        def first():
            return ws_ns.Nav()

        with pytest.raises(ValueError, match="already declared"):

            @ws_ns.navigation
            def second():
                return ws_ns.Nav()

    def test_navigation_must_return_a_nav_panel(self):
        from frontflow import workspace as ws_ns

        with pytest.raises(TypeError, match="must return a"):

            @ws_ns.navigation
            def not_a_nav():
                return displays.Markdown("nope")
