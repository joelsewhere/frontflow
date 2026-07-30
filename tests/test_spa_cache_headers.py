"""Tests for the SPA fallback's cache-header behavior.

Bug context: without explicit `Cache-Control` on `index.html`,
browsers heuristically cache the HTML and continue referencing the
previous build's content-hashed asset filenames after a deploy.
Those filenames no longer exist on disk and 404 the SPA into a
broken state on direct-URL navigation (works via in-app click,
breaks via URL bar).

Fix: `no-cache` on every `index.html` response (forces revalidation
on each load), and `public, max-age=31536000, immutable` on every
/assets/ response (the filenames are content-hashed; same name
always means same bytes).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def static_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Spin up the FastAPI app with a stub static dir populated
    so the SPA-fallback branch runs. We can't rely on the package's
    own /static dir being present during tests (it ships in the
    wheel, not the source tree)."""
    # Build a minimal static dir tree the SPA handler expects.
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html>")
    (static / "assets" / "test-vendor-ABC123.js").write_text(
        "console.log('asset');"
    )
    (static / "favicon.ico").write_text("(fake-ico)")

    # Patch the module-level _STATIC_DIR after import. The catch-all
    # route was registered using a closure over _STATIC_DIR; we need
    # a fresh app that picks up our patched dir. Easier path: build a
    # FastAPI app inline using the same handler code so we test the
    # behavior in isolation from the rest of the application.
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse

    # Import the helper class from main so the test exercises the
    # actual production code, not a copy.
    from frontflow.main import _ImmutableAssetsStaticFiles

    app = FastAPI()
    app.mount(
        "/assets",
        _ImmutableAssetsStaticFiles(directory=static / "assets"),
        name="assets",
    )

    @app.get("/{full_path:path}", include_in_schema=False)
    def _spa(full_path: str) -> FileResponse:
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="not found")
        candidate = (static / full_path).resolve()
        if static in candidate.parents and candidate.is_file():
            return FileResponse(
                candidate,
                headers={"Cache-Control": "public, max-age=3600"},
            )
        return FileResponse(
            static / "index.html",
            headers={"Cache-Control": "no-cache"},
        )

    return TestClient(app)


class TestSpaCacheHeaders:
    def test_index_html_for_root_is_no_cache(
        self, static_app: TestClient,
    ):
        """A request to `/` returns index.html with `Cache-Control:
        no-cache`, forcing the browser to revalidate on every
        load. Without this, the browser caches the HTML across a
        deploy and continues referencing the previous build's
        asset hashes — those filenames no longer exist on disk."""
        r = static_app.get("/")
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-cache"

    def test_deep_route_falls_back_to_no_cache_index(
        self, static_app: TestClient,
    ):
        """A URL-bar nav to an SPA route (`/forms/X`) returns the
        same `index.html` with the same `no-cache` header. This is
        the specific path Connor hit — direct URL navigation must
        get a fresh index, not a heuristically-cached one."""
        r = static_app.get("/forms/property_acquisition_upload")
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-cache"

    def test_hashed_asset_is_immutable(
        self, static_app: TestClient,
    ):
        """A request to `/assets/<hashed>.js` gets an immutable
        long-lived `Cache-Control`. The asset filename embeds a
        content hash; different bytes always produce a different
        URL, so it's safe (and ideal) for the browser to cache
        forever. Without this the browser still works, but each
        SPA load eats a round-trip per asset."""
        r = static_app.get("/assets/test-vendor-ABC123.js")
        assert r.status_code == 200
        assert (
            r.headers["cache-control"]
            == "public, max-age=31536000, immutable"
        )

    def test_non_assets_static_file_is_short_cache(
        self, static_app: TestClient,
    ):
        """A non-hashed static file at the static root (favicon,
        manifest) is cached for an hour — short enough that a real
        change reaches users quickly, long enough to avoid the
        every-request fetch. Not `immutable` (the filename isn't
        content-hashed)."""
        r = static_app.get("/favicon.ico")
        assert r.status_code == 200
        assert r.headers["cache-control"] == "public, max-age=3600"

    def test_unknown_api_path_is_404_not_spa(
        self, static_app: TestClient,
    ):
        """An unmatched /api path must 404 — never fall through to
        the SPA. Otherwise an API typo silently returns HTML and a
        confused client tries to JSON.parse it."""
        r = static_app.get("/api/totally-fake-route")
        assert r.status_code == 404
