"""Tests for the synchronous Superset REST client.

Two behaviours here are load-bearing and easy to lose in a refactor,
so they are asserted directly rather than inferred from a happy path:

  1. **CSRF on mutating calls.** Superset rejects POST/PUT/DELETE
     without an `X-CSRFToken` header. Without this, every provisioning
     call fails with a confusing 400.

  2. **Session-cookie replay.** Superset ties the CSRF token to the
     session cookie set alongside it, and a deployment sets
     `SESSION_COOKIE_SECURE = True` (needed so browsers accept the
     `SameSite=None` cookie in-page dashboard editing depends on).
     httpx then correctly refuses to send that Secure cookie over a
     plain-HTTP internal network — so the client reads it from the jar
     and replays it as an explicit header. Drop that and provisioning
     breaks with "CSRF session token is missing", which reads like a
     server misconfiguration rather than a client bug.

The transport is mocked; no Superset is required.
"""
from __future__ import annotations

import json

import httpx
import pytest

from frontflow.dsl import store
from frontflow.superset.client import (
    SupersetClient,
    SupersetError,
    SupersetUnreachable,
)


CSRF = "CSRF-TOKEN-VALUE"
SESSION = "SESSION-COOKIE-VALUE"


class FakeSuperset:
    """A minimal Superset that records every request it receives."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        # (method, path, parsed-json-body) for every request with a body.
        self.bodies: list[tuple[str, str, dict]] = []
        self.metadata: dict[str, str] = {}
        self.embedding: set[str] = set()
        self.dashboards_created = 0
        self.reachable = True

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.reachable:
            raise httpx.ConnectError("connection refused")

        self.requests.append(
            (request.method, request.url.path, dict(request.headers))
        )
        if request.content:
            try:
                self.bodies.append(
                    (request.method, request.url.path, json.loads(request.content))
                )
            except ValueError:
                pass
        path, method = request.url.path, request.method

        if path == "/health":
            return httpx.Response(200, text="OK")
        if path == "/api/v1/security/login":
            return httpx.Response(200, json={"access_token": "ACCESS"})
        if path == "/api/v1/security/csrf_token/":
            # Superset sets the session cookie on THIS response.
            return httpx.Response(
                200,
                json={"result": CSRF},
                headers={"set-cookie": f"session={SESSION}; Path=/; Secure"},
            )
        if path == "/api/v1/me/":
            return httpx.Response(200, json={"result": {"username": "admin"}})
        if path == "/api/v1/security/guest_token/":
            return httpx.Response(200, json={"token": "GUEST-JWT"})
        if path == "/api/v1/dashboard/" and method == "POST":
            self.dashboards_created += 1
            return httpx.Response(201, json={"id": 100 + self.dashboards_created})
        if path == "/api/v1/dashboard/" and method == "GET":
            return httpx.Response(
                200,
                json={
                    "result": [
                        {"id": 7, "dashboard_title": "Sales", "status": "published"}
                    ]
                },
            )
        if path.endswith("/embedded"):
            dashboard_id = path.split("/dashboard/")[1].split("/")[0]
            if method == "POST":
                self.embedding.add(dashboard_id)
                return httpx.Response(
                    200, json={"result": {"uuid": f"UUID-{dashboard_id}"}}
                )
            result = (
                {"uuid": f"UUID-{dashboard_id}"}
                if dashboard_id in self.embedding
                else {}
            )
            return httpx.Response(200, json={"result": result})
        if path.startswith("/api/v1/dashboard/") and method == "PUT":
            body = json.loads(request.content)
            self.metadata[path.rsplit("/", 1)[1]] = body["json_metadata"]
            return httpx.Response(200, json={"result": {}})
        if path.startswith("/api/v1/dashboard/") and method == "GET":
            key = path.rsplit("/", 1)[1]
            return httpx.Response(
                200, json={"result": {"json_metadata": self.metadata.get(key)}}
            )
        if path == "/api/v1/dataset/" and method == "GET":
            return httpx.Response(200, json={"result": []})
        if path == "/api/v1/dataset/" and method == "POST":
            return httpx.Response(201, json={"id": 55})
        if path == "/api/v1/database/":
            return httpx.Response(200, json={"result": [{"id": 1}]})
        return httpx.Response(404, json={})

    def sent(self, method: str, path: str) -> dict[str, str]:
        """Headers of the first matching request."""
        for m, p, headers in self.requests:
            if m == method and p == path:
                return headers
        raise AssertionError(f"no {method} {path} was sent")

    def body(self, method: str, path: str) -> dict:
        """Parsed JSON body of the first matching request."""
        for m, p, payload in self.bodies:
            if m == method and p == path:
                return payload
        raise AssertionError(f"no {method} {path} body was sent")


@pytest.fixture
def superset(app) -> FakeSuperset:
    """A fake Superset plus a stored connection pointing at it.

    Depends on `app` for the truncated-DB isolation every test gets.
    """
    store.upsert_connection(
        name="superset_default",
        conn_type="superset",
        base_url="http://superset.test:8088",
        auth_kind="basic",
        secret={"username": "admin", "password": "hunter2"},
    )
    # The access token is cached per (base_url, username) across
    # instances; clear it so each test starts from a real login.
    SupersetClient._token_cache.clear()
    return FakeSuperset()


@pytest.fixture
def client(superset: FakeSuperset):
    c = SupersetClient()
    c._http = httpx.Client(
        transport=httpx.MockTransport(superset.handler), timeout=5
    )
    with c:
        yield c


class TestCredentials:
    def test_credentials_come_from_the_connection_store(self, client):
        """Not the environment — so the password is Fernet-encrypted at
        rest and an install can address more than one Superset."""
        assert client.base_url == "http://superset.test:8088"
        assert client.connection_name == "superset_default"

    def test_missing_connection_is_a_clear_error(self, app):
        """A Superset that was never configured should say so, rather
        than failing later with a confusing HTTP error."""
        SupersetClient._token_cache.clear()
        with pytest.raises(Exception) as excinfo:
            SupersetClient("nope_not_configured")
        assert "not configured" in str(excinfo.value)


class TestCsrfAndSessionReplay:
    def test_mutating_call_carries_csrf_token(self, client, superset):
        """Superset rejects mutating API calls without X-CSRFToken."""
        client.create_dashboard("Sales")
        headers = superset.sent("POST", "/api/v1/dashboard/")
        assert headers.get("x-csrftoken") == CSRF

    def test_mutating_call_replays_the_session_cookie(self, client, superset):
        """The CSRF token is only accepted alongside its session cookie,
        which httpx will not send itself over plain HTTP because the
        cookie is marked Secure. See this module's docstring."""
        client.create_dashboard("Sales")
        headers = superset.sent("POST", "/api/v1/dashboard/")
        assert headers.get("cookie") == f"session={SESSION}"

    def test_read_calls_do_not_carry_csrf(self, client, superset):
        """Read paths need no CSRF; sending it anyway would be noise."""
        client.list_dashboards()
        headers = superset.sent("GET", "/api/v1/dashboard/")
        assert "x-csrftoken" not in headers


class TestProvisioningCalls:
    def test_create_dashboard_returns_the_new_id(self, client):
        assert client.create_dashboard("Sales") == "101"

    def test_ensure_dataset_creates_when_absent(self, client):
        """The fake reports no matching dataset, so one is created."""
        assert client.ensure_dataset("v_frontflow_submissions", "FrontFlow") == 55

    def test_ensure_time_filter_targets_created_at(self, client):
        """The refresh operator drives a time-range filter on
        `created_at`; anything else would not move the query cache key."""
        dashboard_id = client.create_dashboard("Auto")
        filter_id = client.ensure_time_filter(dashboard_id, 55)

        metadata = client.get_json_metadata(dashboard_id)
        native = metadata["native_filter_configuration"][0]
        assert native["id"] == filter_id
        assert native["filterType"] == "filter_time"
        assert native["targets"][0]["column"]["name"] == "created_at"

    def test_ensure_time_filter_is_idempotent(self, client):
        """Re-provisioning must not accumulate duplicate filters — the
        dashboard would then carry one per deploy."""
        dashboard_id = client.create_dashboard("Auto")
        first = client.ensure_time_filter(dashboard_id, 55)
        second = client.ensure_time_filter(dashboard_id, 55)

        metadata = client.get_json_metadata(dashboard_id)
        assert first == second
        assert len(metadata["native_filter_configuration"]) == 1

    def test_get_embedded_uuid_is_none_before_enabling(self, client):
        """Superset answers 200 with an empty result rather than 404,
        so "not embedded" has to be read from the body."""
        dashboard_id = client.create_dashboard("Auto")
        assert client.get_embedded_uuid(dashboard_id) is None
        client.enable_embedding(dashboard_id, [])
        assert client.get_embedded_uuid(dashboard_id) == f"UUID-{dashboard_id}"


class TestGuestTokens:
    def test_guest_token_is_scoped_to_the_embed_uuid(self, client, superset):
        """The resource id must be the embed UUID, never the numeric
        dashboard id — the numeric id yields a blank iframe and no error,
        which is among the harder embedding failures to diagnose."""
        assert client.guest_token("UUID-101") == "GUEST-JWT"

        body = superset.body("POST", "/api/v1/security/guest_token/")
        assert body["resources"] == [
            {"type": "dashboard", "id": "UUID-101"}
        ]

    def test_guest_token_forwards_rls_rules(self, client, superset):
        """Row-level-security rules are how a guest token is narrowed to
        one user's data; silently dropping them would over-share."""
        client.guest_token(
            "UUID-101", rls=[{"clause": "form_id = 'x'"}], username="alice"
        )
        body = superset.body("POST", "/api/v1/security/guest_token/")
        assert body["rls"] == [{"clause": "form_id = 'x'"}]
        assert body["user"]["username"] == "alice"


class TestFailureModes:
    def test_unreachable_superset_raises_unreachable(self, client, superset):
        """Distinct from SupersetError so callers can tell "down" from
        "refused" — provisioning degrades on the former."""
        superset.reachable = False
        with pytest.raises(SupersetUnreachable):
            client.list_dashboards()

    def test_error_response_raises_superset_error(self, client, superset):
        """A 4xx/5xx from a reachable Superset is an error, not silence.

        `enable_embedding` is used here because the fake answers unknown
        dashboard reads with 200 — as the real Superset does — so a read
        is the wrong probe for this.
        """
        def refuse(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/security/login":
                return httpx.Response(200, json={"access_token": "ACCESS"})
            if request.url.path == "/api/v1/security/csrf_token/":
                return httpx.Response(200, json={"result": CSRF})
            return httpx.Response(500, text="boom")

        client._http = httpx.Client(transport=httpx.MockTransport(refuse))
        with pytest.raises(SupersetError):
            client.enable_embedding("101", [])
