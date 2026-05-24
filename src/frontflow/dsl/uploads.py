"""File-upload storage — the backend for the File and S3File inputs.

`File` is transient: its bytes are held with the submission draft and
handed to backend functions, never persisted to durable storage.
`S3File` persists: its bytes are streamed to S3 and the submission
records a reference (bucket, key, filename, size, content-type).

AWS credentials for S3File resolve in this order:
  1. a stored `aws` connection in the connection store, if any;
  2. boto3's default chain — env vars, shared config, instance role.

boto3 is an optional dependency (the `[s3]` extra) and is imported
lazily, so installs without it still load — only S3File use needs it.
"""

from __future__ import annotations

import io
import re
from typing import Any, Optional

from . import store
from .templating import render


class UploadError(RuntimeError):
    """A file upload could not be stored."""


# --- S3 key templating -----------------------------------------------------


def resolve_s3_key(
    template: str,
    *,
    filename: str,
    steps: dict[str, dict[str, Any]],
) -> str:
    """Resolve an S3File `key` template to an exact object key.

    `{{ steps.<node>.<field> }}` tokens (with filters like `slugify`)
    resolve against `steps` — earlier-step values plus any draft
    values for the upload's own screen. The literal `{filename}`
    placeholder expands to the uploaded file's name (whitespace inside
    the braces is tolerated: `{ filename }` works too). The result is
    used verbatim as the S3 object key.

    A token that resolves to nothing renders empty; the caller should
    reject a key that collapses to blank or to a malformed path.
    """
    # `{filename}` is a plain placeholder, not a Jinja token. Swap it
    # for a sentinel that survives templating, then restore it after —
    # this keeps a filename containing `{{ }}` from being interpreted.
    # Tolerate whitespace inside the braces to match how Jinja itself
    # treats `{{ x }}` and `{{x}}` as equivalent — a common author
    # expectation that, if not honoured, silently leaves the literal
    # `{ filename }` in the key.
    sentinel = "\x00FRONTFLOW_FILENAME\x00"
    staged = re.sub(r"\{\s*filename\s*\}", sentinel, template)
    rendered = render(staged, steps, strict=False)
    key = rendered.replace(sentinel, filename)
    # Any remaining `{...}` single-brace block is an unknown
    # placeholder — almost certainly a typo (e.g. `{file_name}` or
    # `{ FILENAME }`). Refuse rather than write a broken key.
    leftover = re.search(r"\{[^{}]*\}", key)
    if leftover:
        raise ValueError(
            f"Unknown placeholder {leftover.group(0)!r} in S3 key — "
            f"only {{filename}} is recognised. Use {{{{ steps.x.y }}}} "
            f"for step values."
        )
    # Normalise — collapse accidental double slashes, strip a leading
    # slash (S3 keys are not absolute paths).
    key = key.strip().lstrip("/")
    while "//" in key:
        key = key.replace("//", "/")
    return key


# --- AWS credential / client resolution ------------------------------------


def _resolve_aws() -> dict[str, Any]:
    """Resolve the AWS settings for S3File uploads.

    The connection holds credentials only — never a bucket. The bucket
    is the form author's concern, set on each S3File. An empty
    credentials section means "fall back to boto3's default chain".

    Looks up the conventional `aws_default` connection. When that
    connection isn't stored, `AWSConnection.handle_missing_connection`
    returns None — we then return empty credentials and let boto3
    resolve them from its own default chain (env vars, ~/.aws/...,
    instance role, etc.).
    """
    from .connections import AWSConnection
    conn = AWSConnection.resolve(None)  # uses DEFAULT_NAME
    if conn is None:
        return {"credentials": None, "region": None}
    secret = conn.get("secret", {})
    creds = None
    if secret.get("aws_access_key_id") and secret.get(
        "aws_secret_access_key"
    ):
        creds = {
            "aws_access_key_id": secret["aws_access_key_id"],
            "aws_secret_access_key": secret["aws_secret_access_key"],
        }
        if secret.get("aws_session_token"):
            creds["aws_session_token"] = secret["aws_session_token"]
    return {
        "credentials": creds,
        "region": secret.get("region"),
    }


def _s3_client():
    """Build a boto3 S3 client from the resolved credentials.

    Raises UploadError if boto3 is not installed (the `[s3]` extra) or
    if no usable credentials can be found anywhere.
    """
    try:
        import boto3  # noqa: F401
        from botocore.exceptions import BotoCoreError, NoCredentialsError
    except ImportError as e:  # pragma: no cover - depends on install extras
        raise UploadError(
            "S3 uploads need the 's3' extra — install frontflow[s3]."
        ) from e

    aws = _resolve_aws()
    kwargs: dict[str, Any] = {}
    if aws["region"]:
        kwargs["region_name"] = aws["region"]
    if aws["credentials"]:
        kwargs.update(aws["credentials"])

    try:
        client = boto3.client("s3", **kwargs)
    except (BotoCoreError, NoCredentialsError) as e:
        raise UploadError(f"could not initialise S3 client: {e}") from e
    return client


# --- S3 upload --------------------------------------------------------------


def put_s3_object(
    *,
    data: bytes,
    bucket: str,
    key: str,
    filename: str,
    content_type: str,
) -> dict[str, Any]:
    """Stream bytes to S3 at an exact object key and return a
    reference dict.

    `bucket` is required — it comes from the S3File input; the AWS
    connection holds credentials only. `key` is the resolved S3 object
    key, used verbatim — an existing object at that key is overwritten.
    Raises UploadError on any failure (missing bucket, missing
    credentials, S3 error).
    """
    if not bucket or not bucket.strip():
        raise UploadError("S3 upload has no target bucket")
    if not key or not key.strip():
        raise UploadError("S3 upload has no resolved object key")
    client = _s3_client()

    try:
        client.upload_fileobj(
            io.BytesIO(data),
            bucket,
            key,
            ExtraArgs={"ContentType": content_type},
        )
    except Exception as e:  # botocore raises a variety of error types
        raise UploadError(f"S3 upload failed: {e}") from e

    return {
        "bucket": bucket,
        "key": key,
        "filename": filename,
        "size": len(data),
        "content_type": content_type,
    }


def get_s3_bytes(bucket: str, key: str) -> bytes:
    """Fetch an object's bytes back from S3 — backs S3File.read()."""
    client = _s3_client()
    try:
        buf = io.BytesIO()
        client.download_fileobj(bucket, key, buf)
        return buf.getvalue()
    except Exception as e:
        raise UploadError(f"S3 download failed: {e}") from e


def presigned_url(
    bucket: str, key: str, expires_in: int = 3600
) -> str:
    """A time-limited download URL for an S3 object — backs
    S3File.url()."""
    client = _s3_client()
    try:
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires_in,
        )
    except Exception as e:
        raise UploadError(f"could not presign URL: {e}") from e


# --- Backend file handles --------------------------------------------------
#
# What a @backend function receives for a File / S3File field. The
# submitted value is a reference dict; these wrap it in an object the
# function can actually use — .read() into io / pandas, etc.


class FileHandle:
    """A transient `File` upload, as seen by a @backend function.

    The bytes are loaded from the upload blob the moment the handle is
    built. `.bytes` / `.read()` give the raw content; `.filename`,
    `.content_type`, and `.size` describe it. Nothing is persisted —
    once the submission ends, the underlying blob is removed.
    """

    def __init__(
        self,
        *,
        filename: str,
        content_type: str,
        size: int,
        data: bytes,
    ) -> None:
        self.filename = filename
        self.content_type = content_type
        self.size = size
        self._data = data

    @property
    def bytes(self) -> bytes:
        """The raw file content."""
        return self._data

    def read(self) -> bytes:
        """The raw file content — mirrors a file object's .read()."""
        return self._data

    def __repr__(self) -> str:
        return (
            f"FileHandle(filename={self.filename!r}, "
            f"size={self.size})"
        )


class S3FileHandle:
    """A persisted `S3File` upload, as seen by a @backend function.

    Carries the S3 reference — `.bucket`, `.key`, `.filename`,
    `.content_type`, `.size` — and can fetch the content back:
    `.read()` downloads the bytes, `.url()` returns a time-limited
    download link.
    """

    def __init__(
        self,
        *,
        bucket: str,
        key: str,
        filename: str,
        content_type: str,
        size: int,
    ) -> None:
        self.bucket = bucket
        self.key = key
        self.filename = filename
        self.content_type = content_type
        self.size = size

    def read(self) -> bytes:
        """Download and return the object's bytes from S3."""
        return get_s3_bytes(self.bucket, self.key)

    def url(self, expires_in: int = 3600) -> str:
        """A time-limited presigned download URL for the object."""
        return presigned_url(self.bucket, self.key, expires_in)

    def __repr__(self) -> str:
        return (
            f"S3FileHandle(bucket={self.bucket!r}, key={self.key!r}, "
            f"filename={self.filename!r})"
        )


def handle_for_value(field_type: str, value: Any) -> Any:
    """Turn a file field's stored value into the handle a @backend
    function should receive. A non-file field, or an empty value, is
    returned unchanged.
    """
    if value is None or not isinstance(value, dict):
        return value
    if field_type == "file":
        token = value.get("token")
        if token is None:
            return value
        blob = store.get_upload_blob(token)
        if blob is None:
            # The blob is gone (e.g. submission already cleaned up).
            return None
        return FileHandle(
            filename=blob["filename"],
            content_type=blob["content_type"],
            size=blob["size"],
            data=blob["data"],
        )
    if field_type == "s3file":
        if not value.get("bucket") or not value.get("key"):
            return value
        return S3FileHandle(
            bucket=value["bucket"],
            key=value["key"],
            filename=value.get("filename", ""),
            content_type=value.get(
                "content_type", "application/octet-stream"
            ),
            size=value.get("size", 0),
        )
    return value
