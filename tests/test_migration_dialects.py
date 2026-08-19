"""The lightweight column migrations must be valid on every supported
backend, not just SQLite.

Regression: `ALTER TABLE submission ADD COLUMN deleted_at DATETIME`
shipped to a Postgres deployment and crashed the app at startup with
`type "datetime" does not exist` — SQLite accepts DATETIME, Postgres
does not. Any timestamp column added by a migration has to render its
type for the live dialect.
"""
from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import DateTime
from sqlalchemy.dialects import postgresql, sqlite

from frontflow.dsl import store


def test_timestamp_ddl_matches_engine_dialect():
    rendered = store._timestamp_ddl()
    assert rendered  # non-empty
    # Whatever the test engine is, the rendered type must be what that
    # dialect actually accepts.
    expected = DateTime().compile(dialect=store._engine.dialect)
    assert rendered == expected


def test_timestamp_renders_per_dialect():
    assert DateTime().compile(dialect=sqlite.dialect()) == "DATETIME"
    pg = DateTime().compile(dialect=postgresql.dialect())
    assert "TIMESTAMP" in pg.upper()
    assert pg.upper() != "DATETIME"


def test_no_hardcoded_datetime_in_alter_statements():
    """Guard against reintroducing the SQLite-only literal."""
    src = Path(store.__file__).read_text()
    offenders = re.findall(
        r"ADD COLUMN\s+\w+\s+DATETIME", src, flags=re.IGNORECASE,
    )
    assert not offenders, (
        f"hardcoded DATETIME in migration DDL: {offenders} — render the "
        f"type with _timestamp_ddl() so Postgres deployments start"
    )


def test_migration_adds_timestamp_column_on_a_legacy_db(tmp_path):
    """A DB created before `deleted_at` existed gets the column added,
    and the migration is idempotent."""
    from sqlalchemy import create_engine, inspect, text

    db = tmp_path / "legacy.sqlite"
    eng = create_engine(f"sqlite:///{db}")
    # Build the current schema, then take the column away again to
    # stand in for a database created before it existed.
    store.Base.metadata.create_all(eng)
    with eng.begin() as conn:
        # SQLite refuses to drop a column an index references.
        conn.execute(text("DROP INDEX IF EXISTS ix_submission_deleted_at"))
        conn.execute(text("ALTER TABLE submission DROP COLUMN deleted_at"))
    assert "deleted_at" not in {
        c["name"] for c in inspect(eng).get_columns("submission")
    }

    original = store._engine
    try:
        store._engine = eng
        store._migrate_add_columns()
        cols = {c["name"] for c in inspect(eng).get_columns("submission")}
        assert "deleted_at" in cols
        assert "updated_at" in cols
        store._migrate_add_columns()  # idempotent
    finally:
        store._engine = original
