"""Regression tests for `_resolve_database_url`'s Postgres URL
normalization.

Heroku/Render/Railway provision Postgres and set `DATABASE_URL` to a
legacy `postgres://...` string that SQLAlchemy 2.x rejects. We
rewrite it (and the ambiguous `postgresql://...`) to the explicit
`postgresql+psycopg://...` form so the env var works on every
platform without the user knowing about the footgun.

We test the resolution function directly. The module-level
`store.DATABASE_URL` is set once at import and frozen; the function
itself reads env on every call, which is what we exercise.
"""
from __future__ import annotations

import pytest

from frontflow.dsl.store import _resolve_database_url


class TestPostgresUrlNormalization:
    def test_legacy_postgres_scheme_rewritten(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """`postgres://...` (Heroku/Render shape) becomes
        `postgresql+psycopg://...` so SQLAlchemy 2.x accepts it."""
        monkeypatch.setenv(
            "DATABASE_URL", "postgres://user:pw@host:5432/db"
        )
        assert _resolve_database_url() == (
            "postgresql+psycopg://user:pw@host:5432/db"
        )

    def test_ambiguous_postgresql_scheme_rewritten(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """`postgresql://...` (no explicit driver) gets pinned to
        psycopg so behavior matches what the postgres extra installs."""
        monkeypatch.setenv(
            "DATABASE_URL", "postgresql://user:pw@host:5432/db"
        )
        assert _resolve_database_url() == (
            "postgresql+psycopg://user:pw@host:5432/db"
        )

    @pytest.mark.parametrize("url", [
        "postgresql+psycopg://user:pw@host/db",
        "postgresql+asyncpg://user:pw@host/db",
        "postgresql+pg8000://user:pw@host/db",
    ])
    def test_explicit_driver_passes_through(
        self, monkeypatch: pytest.MonkeyPatch, url: str
    ):
        """Users who pick a specific driver (asyncpg, pg8000, or
        psycopg explicitly) must not be silently rewritten."""
        monkeypatch.setenv("DATABASE_URL", url)
        assert _resolve_database_url() == url

    def test_non_postgres_url_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Non-Postgres URLs (MySQL, SQLite, etc.) are untouched."""
        monkeypatch.setenv(
            "DATABASE_URL", "mysql+pymysql://user:pw@host/db"
        )
        assert _resolve_database_url() == (
            "mysql+pymysql://user:pw@host/db"
        )

    def test_special_chars_in_password_preserved(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Rewrite is a prefix swap, not a parse-and-rebuild, so
        weird passwords with @ : / % survive untouched."""
        monkeypatch.setenv(
            "DATABASE_URL", "postgres://user:p%40ss%2Fword@host:5432/db"
        )
        assert _resolve_database_url() == (
            "postgresql+psycopg://user:p%40ss%2Fword@host:5432/db"
        )

    def test_fallback_to_sqlite_when_unset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        """No DATABASE_URL → falls back to a SQLite file. Not part
        of the rewrite per se, but locks in the precedence so a
        future edit doesn't silently break default behavior."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
        url = _resolve_database_url()
        assert url.startswith("sqlite:///")
        assert "test.db" in url
