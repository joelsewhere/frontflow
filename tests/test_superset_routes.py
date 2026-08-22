"""Authorization tests for the Superset dashboard routes.

Minting a guest token hands out read access to a Superset dashboard, so
these routes are the security-sensitive part of the integration. The
prototype this was ported from left its guest-token endpoint deliberately
unauthenticated — a documented local-development compromise. Carrying
that into frontflow, which has users, groups, and per-form ACLs, would be
a regression, so the gating is asserted directly.

Two independent gates apply to the form-scoped routes:

  1. the form's own visibility rules (`require_form_visibility`), and
  2. the dashboard actually appearing in that form.

The second matters on its own: without it, anyone who can see *any* form
could name an arbitrary dashboard and be handed a token for it.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from frontflow.dsl import store
from frontflow.superset.client import SupersetClient

from test_superset_client import FakeSuperset


@pytest.fixture
def superset(app, monkeypatch: pytest.MonkeyPatch) -> FakeSuperset:
    store.upsert_connection(
        name="superset_default",
        conn_type="superset",
        base_url="http://superset.test:8088",
        auth_kind="basic",
        secret={"username": "admin", "password": "hunter2"},
    )
    SupersetClient._token_cache.clear()

    fake = FakeSuperset()
    original_init = SupersetClient.__init__

    def patched_init(self, connection=None):
        original_init(self, connection)
        self._http = httpx.Client(
            transport=httpx.MockTransport(fake.handler), timeout=5
        )

    monkeypatch.setattr(SupersetClient, "__init__", patched_init)
    return fake


# `test_dashboard` is the fixture form carrying
# displays.Dashboard("sales_overview").
FORM = "test_dashboard"
DASH = "sales_overview"
PRIVATE_FORM = "test_private_dashboard"
PRIVATE_DASH = "secret_metrics"


class TestGuestTokenAuthorization:
    def test_private_form_denies_anonymous_a_guest_token(
        self, anon_client: TestClient, superset
    ):
        """The gate that matters: a dashboard on a RESTRICTED form
        inherits that restriction. 404 rather than 403 — a restricted
        form's existence is not leaked by probing, and neither is its
        dashboard's."""
        r = anon_client.post(
            f"/api/forms/{PRIVATE_FORM}/dashboards/{PRIVATE_DASH}/guest-token"
        )
        assert r.status_code == 404, r.text

    def test_private_form_allows_a_permitted_user(
        self, admin_client: TestClient, superset
    ):
        r = admin_client.post(
            f"/api/forms/{PRIVATE_FORM}/dashboards/{PRIVATE_DASH}/guest-token"
        )
        assert r.status_code == 200, r.text
        assert r.json()["token"] == "GUEST-JWT"

    def test_public_form_serves_anonymous_by_design(
        self, anon_client: TestClient, superset
    ):
        """A dashboard is exactly as reachable as the form it sits on.
        A public form is anonymous-fillable by design, so its dashboard
        is anonymous-viewable too — placing a dashboard on a public form
        publishes it. Asserted so the trade is deliberate and visible
        rather than discovered later."""
        r = anon_client.post(
            f"/api/forms/{FORM}/dashboards/{DASH}/guest-token"
        )
        assert r.status_code == 200, r.text

    def test_guest_token_does_not_depend_on_calling_embed_first(
        self, admin_client: TestClient, superset
    ):
        """The SDK re-invokes its token callback on its own schedule, so
        the two calls must not be order-dependent."""
        r = admin_client.post(
            f"/api/forms/{FORM}/dashboards/{DASH}/guest-token"
        )
        assert r.status_code == 200, r.text

    def test_cannot_mint_for_a_dashboard_not_in_the_form(
        self, admin_client: TestClient, superset
    ):
        """Otherwise any form becomes a key to every dashboard."""
        r = admin_client.post(
            f"/api/forms/{FORM}/dashboards/some_other_dashboard/guest-token"
        )
        assert r.status_code == 404, r.text

    def test_unknown_form_does_not_leak(
        self, admin_client: TestClient, superset
    ):
        r = admin_client.post(
            f"/api/forms/no_such_form/dashboards/{DASH}/guest-token"
        )
        assert r.status_code == 404, r.text


class TestEmbedConfig:
    def test_embed_config_resolves_the_name(
        self, admin_client: TestClient, superset
    ):
        """First request provisions, so a referenced dashboard works
        against an empty Superset."""
        r = admin_client.get(f"/api/forms/{FORM}/dashboards/{DASH}/embed")
        assert r.status_code == 200, r.text

        body = r.json()
        assert body["name"] == DASH
        assert body["embed_uuid"]
        assert body["filter_id"]
        assert body["superset_domain"] == "http://superset.test:8088"

    def test_public_url_override_is_used_for_the_browser(
        self, admin_client: TestClient, superset, monkeypatch
    ):
        """The connection's base_url is how the *server* reaches
        Superset — often an internal hostname meaningless in a browser."""
        monkeypatch.setenv(
            "FRONTFLOW_SUPERSET_PUBLIC_URL", "https://bi.example.com"
        )
        r = admin_client.get(f"/api/forms/{FORM}/dashboards/{DASH}/embed")
        assert r.json()["superset_domain"] == "https://bi.example.com"

    def test_private_form_denies_anonymous_the_embed_config(
        self, anon_client: TestClient, superset
    ):
        r = anon_client.get(
            f"/api/forms/{PRIVATE_FORM}/dashboards/{PRIVATE_DASH}/embed"
        )
        assert r.status_code == 404, r.text


class TestAdminRoutes:
    def test_listing_bindings_requires_admin(
        self, anon_client: TestClient, superset
    ):
        assert anon_client.get("/api/dashboards").status_code in (401, 403, 503)

    def test_admin_can_list_bindings_with_health(
        self, admin_client: TestClient, superset
    ):
        admin_client.get(f"/api/forms/{FORM}/dashboards/{DASH}/embed")

        r = admin_client.get("/api/dashboards")
        assert r.status_code == 200, r.text
        bindings = r.json()
        assert [b["name"] for b in bindings] == [DASH]
        assert bindings[0]["healthy"] is True

    def test_repair_requires_admin(self, anon_client: TestClient, superset):
        r = anon_client.post(f"/api/dashboards/{DASH}/repair")
        assert r.status_code in (401, 403, 503)

    def test_status_reports_unreachable_without_raising(
        self, admin_client: TestClient, superset
    ):
        """An unreachable Superset is a normal state the admin UI has to
        render, not a 500."""
        superset.reachable = False
        SupersetClient._token_cache.clear()

        r = admin_client.get("/api/superset/status")
        assert r.status_code == 200, r.text
        assert r.json()["reachable"] is False
        assert r.json()["detail"]


class TestLayoutTree:
    """The compiled block is what the frontend actually renders, so the
    contract worth asserting is the JSON a form's step ships — not the
    Python object graph behind it."""

    def _dashboard_blocks(self, tree: dict) -> list[dict]:
        found: list[dict] = []

        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "dashboard":
                    found.append(node)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(tree)
        return found

    def test_dashboard_block_reaches_the_client(
        self, admin_client: TestClient, superset
    ):
        """`GET /api/forms/{id}` ships the landing layout — the tree the
        form-filling UI renders."""
        r = admin_client.get(f"/api/forms/{FORM}")
        assert r.status_code == 200, r.text

        blocks = self._dashboard_blocks(r.json())
        assert len(blocks) == 1, f"expected exactly one dashboard block, got {len(blocks)}"

        props = blocks[0]["props"]
        assert props["name"] == DASH
        assert props["height"] == 420

    def test_block_carries_no_embed_uuid(
        self, admin_client: TestClient, superset
    ):
        """Only the NAME is compiled in. Forms are snapshotted per
        form_version, so a baked-in UUID would pin a form to whichever
        dashboard existed when it was written — and would leak the UUID
        into every version record."""
        r = admin_client.get(f"/api/forms/{FORM}")
        props = self._dashboard_blocks(r.json())[0]["props"]

        assert "embed_uuid" not in props
        assert "filter_id" not in props
        assert set(props) == {
            "name",
            "connection",
            "height",
            "show_filters",
            "filters_expanded",
        }
