"""Row-level security — which rows of a dashboard a person may see.

The clause is applied inside the query, via `rls` on the guest token, so
a viewer cannot widen it by clicking. Filtering and access control stay
separate concerns: a person may still filter freely within their slice.

The load-bearing property is **failing closed**. For an access-control
mechanism, "no clause means no restriction" turns any bug into a silent
data leak, so every way a resolver can fail must deny instead. That is
what `TestFailsClosed` is for, and why it enumerates the failure modes
rather than testing one.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from frontflow.dsl import store
from frontflow.superset import rls
from frontflow.superset.client import SupersetClient

from test_superset_client import FakeSuperset


FORM = "test_dashboard"
DASH = "sales_overview"


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


@pytest.fixture(autouse=True)
def _clean_resolvers():
    """The registry is module-level, like every other DSL registry."""
    saved = dict(rls.RESOLVERS)
    rls.RESOLVERS.clear()
    yield
    rls.RESOLVERS.clear()
    rls.RESOLVERS.update(saved)


class TestDeclaration:
    def test_a_row_filter_needs_a_dashboard_name(self):
        with pytest.raises(ValueError):
            rls.row_filter("")

    def test_one_resolver_per_dashboard(self):
        """Two functions disagreeing about who may see what should not
        be settled by import order."""

        @rls.row_filter("d")
        def first(user):
            return "1 = 1"

        with pytest.raises(ValueError, match="already has a row filter"):

            @rls.row_filter("d")
            def second(user):
                return "1 = 1"

    def test_redeclaring_the_same_function_is_allowed(self):
        """A module re-imported under another name — which the workflow
        scanner does for pinned sources — must not be an error."""

        @rls.row_filter("d")
        def scope(user):
            return "1 = 1"

        rls.row_filter("d")(scope)  # same function, no raise


class TestUngovernedIsUnrestricted:
    def test_no_resolver_means_no_clause(self):
        """Every dashboard that already exists must keep working."""
        assert rls.clause_for("nobody_declared_this", None) is None
        assert rls.rules_for("nobody_declared_this", None) == []

    def test_no_resolver_is_not_the_same_as_a_denying_one(self):
        """The distinction the whole design rests on: absent means
        unrestricted, present-but-failed means denied."""

        @rls.row_filter("d")
        def scope(user):
            return None

        assert rls.clause_for("other", None) is None
        assert rls.clause_for("d", None) == rls.DENY_ALL


class TestFailsClosed:
    """Every way a resolver can fail must deny, not open."""

    def test_a_raising_resolver_denies(self):
        @rls.row_filter("d")
        def scope(user):
            raise RuntimeError("the lookup service is down")

        assert rls.clause_for("d", None) == rls.DENY_ALL

    def test_returning_none_denies(self):
        @rls.row_filter("d")
        def scope(user):
            return None

        assert rls.clause_for("d", None) == rls.DENY_ALL

    def test_returning_a_non_string_denies(self):
        @rls.row_filter("d")
        def scope(user):
            return ["region = 'East'"]

        assert rls.clause_for("d", None) == rls.DENY_ALL

    def test_returning_blank_denies(self):
        """An empty clause would be spliced in as no restriction."""

        @rls.row_filter("d")
        def scope(user):
            return "   "

        assert rls.clause_for("d", None) == rls.DENY_ALL

    def test_the_deny_clause_actually_matches_nothing(self):
        assert rls.DENY_ALL == "1 = 0"


class TestReachesSuperset:
    """The wiring: a declared clause has to arrive on the guest token,
    or all of the above is theatre."""

    def test_the_clause_is_sent_with_the_token(
        self, admin_client: TestClient, superset: FakeSuperset
    ):
        @rls.row_filter(DASH)
        def scope(user):
            return "region = 'East'"

        r = admin_client.post(f"/api/forms/{FORM}/dashboards/{DASH}/guest-token")
        assert r.status_code == 200, r.text

        body = superset.body("POST", "/api/v1/security/guest_token/")
        assert body["rls"] == [{"clause": "region = 'East'"}]

    def test_an_ungoverned_dashboard_sends_no_rules(
        self, admin_client: TestClient, superset: FakeSuperset
    ):
        r = admin_client.post(f"/api/forms/{FORM}/dashboards/{DASH}/guest-token")
        assert r.status_code == 200, r.text
        assert superset.body("POST", "/api/v1/security/guest_token/")["rls"] == []

    def test_a_failing_resolver_sends_a_denying_clause(
        self, admin_client: TestClient, superset: FakeSuperset
    ):
        """Not an empty list — that would hand over everything."""

        @rls.row_filter(DASH)
        def scope(user):
            raise RuntimeError("boom")

        r = admin_client.post(f"/api/forms/{FORM}/dashboards/{DASH}/guest-token")
        assert r.status_code == 200, r.text
        assert superset.body("POST", "/api/v1/security/guest_token/")["rls"] == [
            {"clause": rls.DENY_ALL}
        ]

    def test_the_resolver_receives_the_acting_user(
        self, admin_client: TestClient, superset: FakeSuperset
    ):
        """Without the user there is nothing to scope by, and every
        viewer would get the same slice."""
        seen: list = []

        @rls.row_filter(DASH)
        def scope(user):
            seen.append(user)
            return f"owner = '{getattr(user, 'username', '?')}'"

        admin_client.post(f"/api/forms/{FORM}/dashboards/{DASH}/guest-token")
        assert seen, "the resolver was never called"
        assert getattr(seen[0], "username", None), "no user was resolved"
