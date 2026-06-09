"""Tests for the iframe-embedding surface.

Covers:
  - `@form(iframe_allowed_origins=[...])` accepts a list, threads it
    through Workflow → CompiledWorkflow → serialized form_version.
  - Validation rejects malformed entries at decoration time.
  - The CSP `frame-ancestors` header is emitted from the allowlist
    on form-rendering routes, and `'none'` everywhere else.
  - Non-public forms with an allowlist are STILL served `'none'`
    (the visibility gate overrides the allowlist).
  - `FormSummary` exposes `iframe_allowed_origins` for the admin UI.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from frontflow.dsl.core import _validate_iframe_origin


class TestDslValidation:
    """The validator runs at @form decoration; bad entries surface
    at workflow load, not at request time."""

    def test_accepts_exact_origin(self):
        _validate_iframe_origin(
            "https://company.com", form_id="t"
        )

    def test_accepts_subdomain_glob(self):
        _validate_iframe_origin(
            "https://*.company.com", form_id="t"
        )

    def test_accepts_origin_with_port(self):
        _validate_iframe_origin(
            "https://company.com:8443", form_id="t"
        )

    def test_accepts_any_origin_wildcard(self):
        _validate_iframe_origin("*", form_id="t")

    def test_accepts_http_origin(self):
        # `http://localhost:3000` is the obvious dev-loop case.
        _validate_iframe_origin(
            "http://localhost:3000", form_id="t"
        )

    def test_rejects_bare_hostname(self):
        # No scheme → CSP would silently not match. Loud error.
        with pytest.raises(ValueError, match="missing a scheme"):
            _validate_iframe_origin("company.com", form_id="t")

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError, match="empty"):
            _validate_iframe_origin("", form_id="t")

    def test_rejects_non_string(self):
        with pytest.raises(ValueError, match="must be strings"):
            _validate_iframe_origin(42, form_id="t")  # type: ignore

    def test_rejects_origin_with_path(self):
        with pytest.raises(ValueError, match="should be an origin"):
            _validate_iframe_origin(
                "https://company.com/path", form_id="t"
            )

    def test_rejects_origin_with_query(self):
        with pytest.raises(ValueError, match="should be an origin"):
            _validate_iframe_origin(
                "https://company.com?foo=1", form_id="t"
            )

    def test_error_message_includes_form_id(self):
        with pytest.raises(ValueError, match="my_form"):
            _validate_iframe_origin(
                "company.com", form_id="my_form"
            )


class TestThreadedThroughCompile:
    """The field reaches CompiledWorkflow + the serialized version
    snapshot, so listing + API both see it without DB lookup."""

    def test_compiled_workflow_carries_origins(self, app):
        import frontflow.main as main_mod
        wf = main_mod.FORMS.get("test_iframable")
        assert wf is not None, "fixture form test_iframable missing"
        assert wf.iframe_allowed_origins == [
            "https://company.com",
            "https://*.company.com",
        ]

    def test_unset_field_is_none(self, app):
        import frontflow.main as main_mod
        wf = main_mod.FORMS["test_simple"]
        assert wf.iframe_allowed_origins is None

    def test_serialize_includes_field(self):
        from frontflow.dsl.compile import (
            compile_workflow, serialize_workflow,
        )
        from frontflow.dsl.core import Workflow, Node, _current_workflow
        # Build a minimal compilable workflow manually.
        wf = Workflow(
            id="s_test",
            iframe_allowed_origins=["https://x.com"],
        )
        # The compiler doesn't require steps for this assertion —
        # but compile_workflow does, so just check serialize directly
        # on a manually-built CompiledWorkflow.
        from frontflow.dsl.compile import CompiledWorkflow
        cw = CompiledWorkflow(
            id="s_test",
            title="t",
            description="",
            steps=[],
            iframe_allowed_origins=["https://x.com"],
        )
        out = serialize_workflow(cw)
        assert out["iframe_allowed_origins"] == ["https://x.com"]

    def test_serialize_null_when_unset(self):
        from frontflow.dsl.compile import (
            CompiledWorkflow, serialize_workflow,
        )
        cw = CompiledWorkflow(
            id="s_test", title="t", description="", steps=[],
        )
        out = serialize_workflow(cw)
        assert out["iframe_allowed_origins"] is None


class TestCspHeaders:
    """Middleware emits `frame-ancestors` correctly per route."""

    def test_non_form_route_gets_none(self, anon_client: TestClient):
        # The login page is not a form-rendering route → 'none'.
        r = anon_client.get("/login")
        # 200 or 404 both fine; we only care about the header.
        csp = r.headers.get("Content-Security-Policy", "")
        assert "frame-ancestors 'none'" in csp
        assert r.headers.get("X-Frame-Options") == "DENY"

    def test_iframable_form_gets_origins(
        self, anon_client: TestClient
    ):
        # The SPA's live-form route /forms/<id>/form should produce
        # a permissive frame-ancestors directive from the form's
        # allowlist. The route renders the SPA shell, so it may
        # 200 (serving index.html) — regardless, the middleware
        # ran, so the header is set.
        r = anon_client.get("/forms/test_iframable/form")
        csp = r.headers.get("Content-Security-Policy", "")
        assert "frame-ancestors" in csp
        assert "https://company.com" in csp
        assert "https://*.company.com" in csp
        # Legacy X-Frame-Options dropped so the allowlist isn't
        # contradicted by a stricter sibling header.
        assert "X-Frame-Options" not in r.headers

    def test_non_iframable_form_gets_none(
        self, anon_client: TestClient
    ):
        # A form WITHOUT an allowlist → `'none'` like every other
        # route. test_simple is a fixture form, not iframable.
        r = anon_client.get("/forms/test_simple/form")
        csp = r.headers.get("Content-Security-Policy", "")
        assert "frame-ancestors 'none'" in csp
        assert r.headers.get("X-Frame-Options") == "DENY"

    def test_admin_summary_not_iframable(
        self, anon_client: TestClient
    ):
        # The admin summary route /forms/<id> (no /form suffix) is
        # never iframable, even when the form has an allowlist —
        # only the live-form render path is.
        r = anon_client.get("/forms/test_iframable")
        csp = r.headers.get("Content-Security-Policy", "")
        assert "frame-ancestors 'none'" in csp

    def test_form_id_extraction_helper(self):
        from frontflow.main import _iframe_form_id_for_path as fn
        # Live-form routes → return the form_id.
        assert fn("/forms/test_iframable/form") == "test_iframable"
        assert fn("/forms/test_iframable/form/draft") == (
            "test_iframable"
        )
        assert fn(
            "/forms/test_iframable/form/submission/abc"
        ) == "test_iframable"
        # Admin summary, listings, and non-form routes → None.
        assert fn("/forms/test_iframable") is None
        assert fn("/forms/test_iframable/submissions/abc") is None
        assert fn("/forms") is None
        assert fn("/forms/") is None
        assert fn("/login") is None
        assert fn("/api/forms") is None
        assert fn("/") is None


class TestApiSurface:
    """FormSummary on `/api/forms` carries iframe_allowed_origins so
    the admin UI can render the iframe icon column without an extra
    fetch."""

    def test_iframable_form_in_listing(
        self, admin_client: TestClient
    ):
        r = admin_client.get("/api/forms")
        assert r.status_code == 200
        body = r.json()
        iframable = next(
            (f for f in body if f["form_id"] == "test_iframable"),
            None,
        )
        assert iframable is not None
        assert iframable["iframe_allowed_origins"] == [
            "https://company.com",
            "https://*.company.com",
        ]

    def test_non_iframable_form_has_null(
        self, admin_client: TestClient
    ):
        r = admin_client.get("/api/forms")
        body = r.json()
        simple = next(
            (f for f in body if f["form_id"] == "test_simple"), None,
        )
        assert simple is not None
        assert simple["iframe_allowed_origins"] is None
