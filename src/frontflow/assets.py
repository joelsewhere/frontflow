"""Sibling assets for a workflow file.

A form is often more than one file: SQL statements, a JSON lookup, a
template. Those siblings must resolve the same way whether the form was
served from a local directory or from S3 — so a form can't just do
`Path(__file__).parent / "sql"`, which is meaningless for an object
fetched from a bucket.

`Assets` closes that gap. Anchor it on the module's `__file__` (the
loader sets that to the file's canonical origin — an absolute path for
local sources, an `s3://bucket/key` URI for S3 ones) and read siblings
by relative path:

    from frontflow import Assets

    ASSETS = Assets(__file__)

    @backend
    def transform(steps):
        sql = ASSETS.read_text("sql/transform.sql")
        ...

The same form file then runs unchanged from `./forms` in development
and from `s3://my-bucket/forms/` in production.

Reads are cached per instance. Because the server re-executes form
files on every rescan (`POST /refresh`), a module-level `Assets` is
rebuilt then too — so edited assets are picked up by a refresh, with
no restart and no stale-cache surprise.
"""

from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Optional


class Assets:
    """Reader for files sitting next to a workflow file.

    `origin` is the form's canonical location — pass `__file__`.
    Accepts an absolute or relative filesystem path, or an
    `s3://bucket/key` URI.

    `connection` names a stored AWS connection for S3 origins
    (defaults to the conventional `aws_default`, falling back to
    boto3's default credential chain) — same resolution as `S3Hook`.
    """

    def __init__(
        self, origin: str, *, connection: Optional[str] = None,
    ) -> None:
        self.origin = str(origin)
        self._connection = connection
        self._cache: dict[str, bytes] = {}
        self._hook = None  # lazily built S3Hook, S3 origins only

    # --- location -------------------------------------------------------

    @property
    def is_s3(self) -> bool:
        return self.origin.startswith("s3://")

    def _s3_parts(self) -> tuple[str, str]:
        """(bucket, key-prefix-of-the-form's-directory) for an S3 origin."""
        rest = self.origin[len("s3://"):]
        bucket, _, key = rest.partition("/")
        return bucket, posixpath.dirname(key)

    def locate(self, relpath: str) -> str:
        """Where `relpath` resolves to — an absolute filesystem path or
        an `s3://` URI. Useful in error messages and logging."""
        rel = self._normalize(relpath)
        if self.is_s3:
            bucket, base = self._s3_parts()
            return f"s3://{bucket}/{posixpath.join(base, rel)}"
        return str((Path(self.origin).resolve().parent / rel))

    @staticmethod
    def _normalize(relpath: str) -> str:
        """Reject absolute paths and parent-escapes — assets live under
        the form's own directory."""
        rel = str(relpath).replace("\\", "/").strip("/")
        if posixpath.isabs(relpath) or ".." in rel.split("/"):
            raise ValueError(
                f"asset path must be relative to the form file and may "
                f"not escape its directory; got {relpath!r}"
            )
        return rel

    # --- reads ----------------------------------------------------------

    def read_bytes(self, relpath: str) -> bytes:
        rel = self._normalize(relpath)
        if rel in self._cache:
            return self._cache[rel]
        if self.is_s3:
            bucket, base = self._s3_parts()
            data = self._s3_hook().read_bytes(
                bucket=bucket, key=posixpath.join(base, rel),
            )
        else:
            data = (Path(self.origin).resolve().parent / rel).read_bytes()
        self._cache[rel] = data
        return data

    def read_text(self, relpath: str, encoding: str = "utf-8") -> str:
        return self.read_bytes(relpath).decode(encoding)

    def exists(self, relpath: str) -> bool:
        rel = self._normalize(relpath)
        if rel in self._cache:
            return True
        if self.is_s3:
            try:
                self.read_bytes(rel)
                return True
            except Exception:  # noqa: BLE001 — missing key / no access
                return False
        return (Path(self.origin).resolve().parent / rel).exists()

    def clear_cache(self) -> None:
        self._cache.clear()

    def _s3_hook(self):
        if self._hook is None:
            from frontflow.aws.hooks import S3Hook  # lazy: boto3 extra

            self._hook = S3Hook(self._connection)
        return self._hook

    def __repr__(self) -> str:
        return f"<Assets origin={self.origin!r}>"
