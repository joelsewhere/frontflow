"""Hooks for AWS services. A hook wraps a stored AWS connection (or
falls back to boto3's default credential chain) and exposes typed
read/write helpers, so a `@backend` doesn't have to wire credentials
itself.

    from frontflow.aws.hooks import S3Hook

    @backend
    def fetch_codes(steps):
        hook = S3Hook()
        return hook.read_json(
            bucket="my-bucket",
            key=f"runs/{steps.start.run_id}/codes.json",
        )

The hook resolves credentials at construction time via
`AWSConnection`. By default it looks up the conventional
`aws_default` connection; pass `connection_name=` to use a different
stored connection. A missing connection falls through to boto3's
default chain (env vars, `~/.aws/credentials`, an instance/role
profile) — same semantics as `S3File` uploads.
"""

from __future__ import annotations

import io
import json
from typing import Any, Optional

from frontflow.dsl.connections import AWSConnection


class S3Hook:
    """Read/write to S3 from a `@backend` with frontflow-resolved
    credentials.

    `connection_name` defaults to the conventional `aws_default`;
    when not found in the connection store, boto3's default credential
    chain is used.
    """

    def __init__(self, connection_name: Optional[str] = None) -> None:
        # Resolve credentials once at construction; the resulting boto3
        # client is reused across calls on this hook.
        rec = AWSConnection.resolve(connection_name)
        # Lazy import — boto3 is an installation extra; the hook only
        # forces the import when someone actually constructs one.
        try:
            import boto3  # type: ignore[import]
        except ImportError as e:  # pragma: no cover - install-time error
            raise ImportError(
                "frontflow.aws.hooks.S3Hook requires boto3 — install "
                "with `pip install boto3` (or the frontflow[aws] extra)."
            ) from e

        kwargs: dict[str, Any] = {}
        if rec is not None:
            secret = rec.get("secret", {})
            if (
                secret.get("aws_access_key_id")
                and secret.get("aws_secret_access_key")
            ):
                kwargs["aws_access_key_id"] = secret["aws_access_key_id"]
                kwargs["aws_secret_access_key"] = secret[
                    "aws_secret_access_key"
                ]
                if secret.get("aws_session_token"):
                    kwargs["aws_session_token"] = secret[
                        "aws_session_token"
                    ]
            if secret.get("region"):
                kwargs["region_name"] = secret["region"]
        # Force SigV4 on every request. boto3's default signature
        # version is the older SigV2, which is rejected outright by
        # any AWS region created after January 2014 (us-east-2,
        # eu-west-2, ca-central-1, ...) and by buckets that use KMS-
        # SSE or object lock. SigV4 is universally accepted, so we
        # use it across the board — no per-region branching.
        # Without this, presigned-URL downloads return:
        #   <Error><Code>InvalidRequest</Code><Message>The
        #   authorization mechanism you have provided is not
        #   supported. Please use AWS4-HMAC-SHA256.</Message></Error>
        from botocore.config import Config  # noqa: PLC0415
        kwargs["config"] = Config(signature_version="s3v4")
        self._client = boto3.client("s3", **kwargs)

    def read_bytes(self, *, bucket: str, key: str) -> bytes:
        """The full object body as bytes. Raises on a missing object
        (boto3's `NoSuchKey` / `ClientError`)."""
        obj = self._client.get_object(Bucket=bucket, Key=key)
        return obj["Body"].read()

    def read_json(self, *, bucket: str, key: str) -> Any:
        """Read the object and parse its body as JSON. Returns whatever
        `json.loads` produces (dict / list / scalar). Raises on parse
        failure or a missing object."""
        return json.loads(self.read_bytes(bucket=bucket, key=key))

    def read_csv(self, *, bucket: str, key: str, **read_csv_kwargs: Any) -> Any:
        """Read the object and parse its body as a pandas DataFrame.

        `read_csv_kwargs` pass through to `pandas.read_csv` — `dtype`,
        `parse_dates`, `header`, `usecols`, etc. pandas is a soft
        dependency: this method imports it lazily and raises a clear
        error if it isn't installed."""
        try:
            import pandas as pd  # type: ignore[import]
        except ImportError as e:  # pragma: no cover - install-time error
            raise ImportError(
                "S3Hook.read_csv requires pandas — install with "
                "`pip install pandas` (or the frontflow[aws] extra)."
            ) from e
        body = self.read_bytes(bucket=bucket, key=key)
        return pd.read_csv(io.BytesIO(body), **read_csv_kwargs)

    def presigned_get_url(
        self,
        *,
        bucket: str,
        key: str,
        expires_in: int = 3600,
        response_content_disposition: Optional[str] = None,
    ) -> str:
        """A presigned URL for a GET on the object. Use this to hand a
        download link to the user without proxying bytes through the
        form server. `expires_in` is in seconds (default 1 hour).

        `response_content_disposition`, when set, is signed into the
        URL as the S3 `response-content-disposition` query parameter.
        S3 echoes it back as the Content-Disposition header on the
        response — which the browser uses to pick the saved filename.
        Typical value: `attachment; filename="my_report.csv"`. Leave
        unset to let the browser fall back to the key's basename.
        """
        params: dict[str, Any] = {"Bucket": bucket, "Key": key}
        if response_content_disposition is not None:
            params["ResponseContentDisposition"] = (
                response_content_disposition
            )
        return self._client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=expires_in,
        )

    def write_bytes(
        self,
        *,
        bucket: str,
        key: str,
        body: bytes,
        content_type: Optional[str] = None,
    ) -> dict[str, Any]:
        """Write `body` to S3. The author sets `content_type` when it
        matters (e.g. `application/json`, `image/png`); when left
        unset, S3 stores the object without an explicit Content-Type
        and serves it as `binary/octet-stream` on read.

        Returns `{bucket, key, etag, version_id}` — the etag is always
        present (from `put_object`'s response); `version_id` is only
        populated when the bucket has versioning enabled, else None.
        """
        extra: dict[str, Any] = {}
        if content_type is not None:
            extra["ContentType"] = content_type
        resp = self._client.put_object(
            Bucket=bucket, Key=key, Body=body, **extra,
        )
        return {
            "bucket": bucket,
            "key": key,
            "etag": (resp.get("ETag") or "").strip('"') or None,
            "version_id": resp.get("VersionId"),
        }

    def write_json(
        self, *, bucket: str, key: str, value: Any,
    ) -> dict[str, Any]:
        """Serialize `value` to JSON and write. Sets the object's
        Content-Type to `application/json`. Mirrors `read_json`."""
        return self.write_bytes(
            bucket=bucket, key=key,
            body=json.dumps(value).encode(),
            content_type="application/json",
        )

    def write_csv(
        self, *, bucket: str, key: str, df: Any, **to_csv_kwargs: Any,
    ) -> dict[str, Any]:
        """Write a pandas DataFrame as CSV. `to_csv_kwargs` pass
        through to `DataFrame.to_csv` — `index`, `header`, `sep`,
        `date_format`, etc. Sets Content-Type to `text/csv`. Mirrors
        `read_csv`. pandas is a soft dependency: this method imports
        it lazily and raises a clear error if it isn't installed."""
        try:
            import pandas as pd  # noqa: F401 — verifies the import
        except ImportError as e:  # pragma: no cover - install-time error
            raise ImportError(
                "S3Hook.write_csv requires pandas — install with "
                "`pip install pandas` (or the frontflow[aws] extra)."
            ) from e
        buf = io.StringIO()
        df.to_csv(buf, **to_csv_kwargs)
        return self.write_bytes(
            bucket=bucket, key=key,
            body=buf.getvalue().encode(),
            content_type="text/csv",
        )
