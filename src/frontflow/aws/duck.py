"""DuckDB with frontflow-resolved S3 credentials.

`DuckDBHook` is the SQL sibling of `S3Hook`: it opens an in-memory
DuckDB connection, loads the `httpfs` extension, and installs an S3
secret from the same credential source `S3Hook` uses — the stored
`aws_default` connection (or `connection_name=`), falling back to the
provider credential chain (env vars, `~/.aws/credentials`, an
instance/role profile) when no stored connection exists.

A `@backend` can then query s3:// flat files directly:

    from frontflow.aws.duck import DuckDBHook

    @backend
    def weekly_counts(steps):
        hook = DuckDBHook()
        rows = hook.execute(
            \"\"\"
            SELECT strftime(date_trunc('week', "Lease - Completed"),
                            '%Y-%m-%d') AS week, COUNT(*) AS n
            FROM read_csv($src, header = true)
            WHERE "Lease - Completed" IS NOT NULL
            GROUP BY 1 ORDER BY 1
            \"\"\",
            {"src": "s3://my-bucket/runs/abc/validated.csv"},
        ).fetchall()
        return {week: n for week, n in rows}

`COPY (SELECT ...) TO $out` writes back to S3 through the same
credentials — a full read-transform-write step with no bytes proxied
through Python.

duckdb is an optional dependency (`pip install frontflow[duckdb]`),
imported lazily like boto3 in `S3Hook`.
"""

from __future__ import annotations

from typing import Any, Optional

from frontflow.dsl.connections import AWSConnection


def _sql_literal(value: str) -> str:
    """Escape a value for embedding in a DuckDB string literal.
    Needed because prepared-statement parameters are not allowed in
    DDL (`CREATE SECRET`)."""
    return "'" + value.replace("'", "''") + "'"


class DuckDBHook:
    """An in-memory DuckDB connection wired for s3:// access with
    frontflow-resolved credentials.

    `connection_name` defaults to the conventional `aws_default`;
    when not found in the connection store, the DuckDB `aws`
    extension's credential chain is used — the same fallback
    semantics as `S3Hook`'s boto3 default chain.
    """

    def __init__(self, connection_name: Optional[str] = None) -> None:
        rec = AWSConnection.resolve(connection_name)
        try:
            import duckdb  # type: ignore[import]
        except ImportError as e:  # pragma: no cover - install-time error
            raise ImportError(
                "frontflow.aws.duck.DuckDBHook requires duckdb — "
                "install with `pip install duckdb` (or the "
                "frontflow[duckdb] extra)."
            ) from e

        self._con = duckdb.connect()
        self._con.execute("INSTALL httpfs; LOAD httpfs;")
        self._con.execute(self._secret_sql(rec))

    @staticmethod
    def _secret_sql(rec: Optional[dict[str, Any]]) -> str:
        """The `CREATE SECRET` statement for a resolved connection
        record. Split out (and static) so tests can check the
        credential wiring without touching S3. Prepared params are
        illegal in DDL, hence the escaped-literal construction."""
        secret = (rec or {}).get("secret", {})
        key_id = secret.get("aws_access_key_id")
        access_key = secret.get("aws_secret_access_key")
        if key_id and access_key:
            parts = [
                "TYPE S3",
                f"KEY_ID {_sql_literal(key_id)}",
                f"SECRET {_sql_literal(access_key)}",
            ]
            if secret.get("aws_session_token"):
                parts.append(
                    "SESSION_TOKEN "
                    + _sql_literal(secret["aws_session_token"])
                )
            if secret.get("region"):
                parts.append(f"REGION {_sql_literal(secret['region'])}")
            body = ", ".join(parts)
            return f"CREATE OR REPLACE SECRET frontflow_s3 ({body});"
        # No stored keys — defer to the provider credential chain
        # (env vars, shared credentials file, instance profile). The
        # chain provider lives in the `aws` extension.
        return (
            "INSTALL aws; LOAD aws; "
            "CREATE OR REPLACE SECRET frontflow_s3 "
            "(TYPE S3, PROVIDER CREDENTIAL_CHAIN);"
        )

    def execute(self, sql: str, params: Optional[dict[str, Any]] = None):
        """Execute SQL with named `$param` bindings. Returns the
        duckdb result — call `.df()`, `.fetchall()`, or `.fetchone()`
        on it. Lists bind natively (e.g. `list_contains($codes, x)`);
        `$param` also binds inside `read_csv($src)` paths and
        `COPY ... TO $out` targets."""
        if params is None:
            return self._con.execute(sql)
        return self._con.execute(sql, params)

    def close(self) -> None:
        self._con.close()
