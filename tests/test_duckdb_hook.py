"""DuckDBHook credential wiring.

The hook's S3 secret must be built from the same connection record
`S3Hook` resolves. `_secret_sql` is static and pure, so the wiring is
testable without opening a DuckDB connection, touching S3, or having
the duckdb package installed.
"""

from frontflow.aws.duck import DuckDBHook, _sql_literal


class TestSecretSql:
    def test_explicit_keys(self):
        sql = DuckDBHook._secret_sql(
            {
                "secret": {
                    "aws_access_key_id": "AKIAEXAMPLE",
                    "aws_secret_access_key": "shhh",
                    "region": "us-east-2",
                }
            }
        )
        assert "TYPE S3" in sql
        assert "KEY_ID 'AKIAEXAMPLE'" in sql
        assert "SECRET 'shhh'" in sql
        assert "REGION 'us-east-2'" in sql
        assert "SESSION_TOKEN" not in sql
        assert "CREDENTIAL_CHAIN" not in sql

    def test_session_token_included_when_present(self):
        sql = DuckDBHook._secret_sql(
            {
                "secret": {
                    "aws_access_key_id": "k",
                    "aws_secret_access_key": "s",
                    "aws_session_token": "tok",
                }
            }
        )
        assert "SESSION_TOKEN 'tok'" in sql

    def test_missing_connection_falls_back_to_chain(self):
        # None record (no stored connection) → provider chain, the
        # same fallback semantics as S3Hook's boto3 default chain.
        sql = DuckDBHook._secret_sql(None)
        assert "PROVIDER CREDENTIAL_CHAIN" in sql
        assert "LOAD aws" in sql

    def test_partial_secret_falls_back_to_chain(self):
        # A record with only a key id (no secret key) can't build an
        # explicit secret — treat like missing.
        sql = DuckDBHook._secret_sql(
            {"secret": {"aws_access_key_id": "k"}}
        )
        assert "PROVIDER CREDENTIAL_CHAIN" in sql

    def test_quote_escaping(self):
        # DDL can't take prepared params, so literals must escape.
        assert _sql_literal("a'b") == "'a''b'"
        sql = DuckDBHook._secret_sql(
            {
                "secret": {
                    "aws_access_key_id": "k",
                    "aws_secret_access_key": "se'cret",
                }
            }
        )
        assert "SECRET 'se''cret'" in sql
