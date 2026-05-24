"""Tests for the Variables tool — install-scoped configuration store.

Covers four layers:

  1. **Store** — encrypt/decrypt round trip, listing returns metadata
     only, missing-variable lookup returns None.
  2. **API** — CRUD via FastAPI TestClient, admin gating, validation.
  3. **Template integration** — `{{ variables.x }}` resolves to stored
     value during render; missing resolves to empty string (matching
     `steps.x.y` semantics).
  4. **Python helper** — `variables.get()` returns value, raises
     MissingVariableError on missing, accepts default.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


class TestVariableStore:
    def test_create_and_read_round_trip(self, app):
        from frontflow.dsl import store
        store.upsert_variable(
            name="bucket_name", value="my-bucket", description="S3 bucket"
        )
        assert store.get_variable("bucket_name") == "my-bucket"

    def test_get_missing_returns_none(self, app):
        from frontflow.dsl import store
        assert store.get_variable("does_not_exist") is None

    def test_list_returns_metadata_only(self, app):
        from frontflow.dsl import store
        store.upsert_variable(name="region", value="us-west-2")
        rows = store.list_variables()
        names = [r["name"] for r in rows]
        assert "region" in names
        # Metadata listing must never include the value — that's the
        # whole point of separating list_* from get_*.
        for row in rows:
            assert "value" not in row
            assert "value_encrypted" not in row

    def test_update_preserves_value_when_value_omitted(self, app):
        from frontflow.dsl import store
        store.upsert_variable(name="hook_url", value="https://x.test")
        store.upsert_variable(
            name="hook_url", value=None, description="Updated desc"
        )
        # Description changed, value preserved.
        assert store.get_variable("hook_url") == "https://x.test"
        meta = store.get_variable_meta("hook_url")
        assert meta["description"] == "Updated desc"

    def test_update_with_new_value_rotates(self, app):
        from frontflow.dsl import store
        store.upsert_variable(name="api_token", value="v1")
        store.upsert_variable(name="api_token", value="v2")
        assert store.get_variable("api_token") == "v2"

    def test_delete_removes(self, app):
        from frontflow.dsl import store
        store.upsert_variable(name="tmp", value="x")
        assert store.delete_variable("tmp") is True
        assert store.get_variable("tmp") is None

    def test_delete_unknown_returns_false(self, app):
        from frontflow.dsl import store
        assert store.delete_variable("never_existed") is False

    def test_create_without_value_raises(self, app):
        from frontflow.dsl import store
        with pytest.raises(ValueError, match="requires a value"):
            store.upsert_variable(name="new_var", value=None)

    def test_get_all_returns_decrypted_map(self, app):
        from frontflow.dsl import store
        store.upsert_variable(name="a", value="alpha")
        store.upsert_variable(name="b", value="beta")
        snapshot = store.get_all_variables()
        assert snapshot["a"] == "alpha"
        assert snapshot["b"] == "beta"

    def test_empty_string_is_a_valid_value(self, app):
        # An empty string is a meaningful "explicitly empty" value
        # — distinct from "not set." Verifies the store doesn't
        # silently coerce "" to None on the way in.
        from frontflow.dsl import store
        store.upsert_variable(name="empty", value="")
        assert store.get_variable("empty") == ""


class TestVariablesAPI:
    def test_list_admin(self, admin_client: TestClient):
        # Seed one variable then list.
        admin_client.put(
            "/api/variables/region",
            json={"value": "us-east-1", "description": "Default AWS region"},
        )
        r = admin_client.get("/api/variables")
        assert r.status_code == 200
        names = [v["name"] for v in r.json()]
        assert "region" in names

    def test_list_response_never_includes_value(
        self, admin_client: TestClient
    ):
        admin_client.put(
            "/api/variables/secret_ish",
            json={"value": "should-not-appear"},
        )
        r = admin_client.get("/api/variables")
        body = r.text
        assert "should-not-appear" not in body, (
            "variable value leaked in list response"
        )

    def test_create_then_read(self, admin_client: TestClient):
        r = admin_client.put(
            "/api/variables/dag_name",
            json={"value": "daily_export", "description": "Pipeline DAG id"},
        )
        assert r.status_code == 200
        assert r.json()["name"] == "dag_name"
        assert r.json()["description"] == "Pipeline DAG id"

        # GET the single record.
        r = admin_client.get("/api/variables/dag_name")
        assert r.status_code == 200
        assert "value" not in r.json(), "value leaked in GET response"

    def test_update_keeps_value_when_omitted(
        self, admin_client: TestClient
    ):
        from frontflow.dsl import store
        admin_client.put("/api/variables/x", json={"value": "first"})
        # Update description only — value omitted.
        admin_client.put(
            "/api/variables/x", json={"description": "added later"}
        )
        # Value preserved via the store-level check.
        assert store.get_variable("x") == "first"

    def test_delete(self, admin_client: TestClient):
        admin_client.put("/api/variables/to_delete", json={"value": "x"})
        r = admin_client.delete("/api/variables/to_delete")
        assert r.status_code == 200
        assert r.json() == {"deleted": True}
        # Subsequent GET → 404.
        r = admin_client.get("/api/variables/to_delete")
        assert r.status_code == 404

    def test_delete_unknown_returns_404(self, admin_client: TestClient):
        r = admin_client.delete("/api/variables/never_existed")
        assert r.status_code == 404

    def test_create_without_value_returns_422(
        self, admin_client: TestClient
    ):
        r = admin_client.put(
            "/api/variables/no_value", json={"description": "d"}
        )
        assert r.status_code == 422

    def test_invalid_name_returns_422(self, admin_client: TestClient):
        # Dashes, dots, leading digits rejected — names need to be
        # valid Python identifiers for the template proxy. Empty
        # string hits a different rejection: the route `/api/
        # variables/{name}` doesn't match `/api/variables/`, so
        # FastAPI 405s on the resulting `/api/variables` PUT. All
        # three are equally fine here — the variable wasn't created.
        for bad in ["with-dash", "1leading_digit", "with.dot"]:
            r = admin_client.put(
                f"/api/variables/{bad}", json={"value": "x"}
            )
            assert r.status_code == 422, f"name {bad!r} was accepted"

    def test_anon_cannot_list(
        self, anon_client: TestClient, admin_user: dict
    ):
        # admin_user fixture ensures at least one account exists, so
        # the response is 401 (auth required) not 503 (bootstrap).
        r = anon_client.get("/api/variables")
        assert r.status_code in (401, 403)

    def test_non_admin_cannot_create(self, user_client: TestClient):
        r = user_client.put(
            "/api/variables/sneaky", json={"value": "x"}
        )
        assert r.status_code in (401, 403)


class TestTemplateResolution:
    """`{{ variables.x }}` in operator templates resolves to the
    stored value. Tests via the templating module directly so the
    integration is verified independent of any operator."""

    def test_renders_stored_variable(self, app):
        from frontflow.dsl import store
        from frontflow.dsl.templating import render
        store.upsert_variable(name="bucket", value="prod-bucket")
        out = render(
            "s3://{{ variables.bucket }}/path",
            steps={},
            variables=store.get_all_variables(),
        )
        assert out == "s3://prod-bucket/path"

    def test_missing_variable_renders_empty(self, app):
        # Matches the `steps.x.y` non-strict semantics — a typo or
        # an as-yet-unset variable becomes empty string, not an
        # exception. The runtime can still detect "did this resolve
        # to something meaningful?" before using the result.
        from frontflow.dsl.templating import render
        out = render(
            "value: '{{ variables.nope }}'",
            steps={},
            variables={},
        )
        assert out == "value: ''"

    def test_variables_and_steps_coexist(self, app):
        from frontflow.dsl.templating import render
        out = render(
            "{{ steps.s1.name }}@{{ variables.host }}",
            steps={"s1": {"name": "alice"}},
            variables={"host": "example.com"},
        )
        assert out == "alice@example.com"


class TestPythonHelper:
    """`variables.get(name)` at workflow load. Distinct from the
    template path: missing variables raise so the workflow fails
    loudly at scan time."""

    def test_returns_stored_value(self, app):
        from frontflow.dsl import store
        from frontflow import variables
        store.upsert_variable(name="env", value="production")
        assert variables.get("env") == "production"

    def test_missing_raises(self, app):
        from frontflow import variables
        with pytest.raises(variables.MissingVariableError):
            variables.get("never_set")

    def test_default_value_used_for_missing(self, app):
        from frontflow import variables
        assert variables.get("absent", default="fallback") == "fallback"

    def test_default_ignored_when_set(self, app):
        from frontflow.dsl import store
        from frontflow import variables
        store.upsert_variable(name="present", value="actual")
        # default is not used when the value exists.
        assert variables.get("present", default="ignored") == "actual"
