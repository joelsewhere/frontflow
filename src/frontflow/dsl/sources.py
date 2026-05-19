"""Workflow sources — where frontflow loads workflow files from.

A workflow source yields workflow *files*: their name, their folder
path (for the console's grouping), and their Python source text. The
server scans whatever source is configured; it does not care whether
the files came from a local directory or an object store.

Two sources ship:

  LocalDirSource   a directory on disk (the default)
  S3Source         an S3 bucket + prefix

A source is built from a URI by `workflow_source_from_uri`:

    ./workflows                  -> LocalDirSource
    /abs/path/to/workflows       -> LocalDirSource
    s3://my-bucket/forms         -> S3Source

S3 support needs boto3, an optional dependency: `pip install frontflow[s3]`.

Note: workflow files are executable Python. A source hands back source
text that the server runs. Point the server only at a location you
control — your own directory, your own bucket.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class WorkflowFile:
    """One workflow file from a source.

    `name` is a stable identifier for the file within the source — a
    relative path like "billing/invoice.py". `folder` is the directory
    part ("" for a top-level file), used by the console to group forms.
    `source` is the file's Python text.
    """

    name: str
    folder: str
    source: str


class WorkflowSource:
    """Where workflow files are loaded from. Subclasses implement
    `iter_files`, yielding every workflow file the source holds."""

    def describe(self) -> str:
        """A short human-readable description, for startup logging."""
        raise NotImplementedError

    def iter_files(self) -> Iterator[WorkflowFile]:
        """Yield every workflow file in the source."""
        raise NotImplementedError

    def fetch(self, name: str) -> WorkflowFile | None:
        """Return one workflow file by name, or None if absent.

        The default walks `iter_files`; a source with cheap point
        lookups (S3) overrides this.
        """
        for wf in self.iter_files():
            if wf.name == name:
                return wf
        return None


class LocalDirSource(WorkflowSource):
    """Workflow files in a local directory tree. `_`-prefixed files and
    anything under __pycache__ are skipped — the established way to
    disable a file without deleting it."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory).expanduser().resolve()

    def describe(self) -> str:
        return f"local directory {self.directory}"

    def iter_files(self) -> Iterator[WorkflowFile]:
        if not self.directory.is_dir():
            return
        for path in sorted(self.directory.rglob("*.py")):
            if path.name.startswith("_") or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(self.directory)
            rel_dir = str(rel.parent)
            folder = "" if rel_dir == "." else rel_dir
            yield WorkflowFile(
                name=str(rel),
                folder=folder,
                source=path.read_text(encoding="utf-8"),
            )


class S3Source(WorkflowSource):
    """Workflow files under an S3 bucket + prefix.

    Every `.py` object under the prefix is a workflow file (excluding
    `_`-prefixed names). Credentials come from the standard AWS chain —
    environment, shared config, or an instance role — so nothing
    secret is passed here.

    Needs boto3: `pip install frontflow[s3]`.
    """

    def __init__(self, bucket: str, prefix: str = "") -> None:
        self.bucket = bucket
        # Normalize: no leading slash, exactly one trailing slash (or "").
        self.prefix = prefix.strip("/")
        if self.prefix:
            self.prefix += "/"

    def _client(self):
        try:
            import boto3  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "S3 workflow source needs boto3 — install it with "
                "`pip install frontflow[s3]`."
            ) from e
        return boto3.client("s3")

    def describe(self) -> str:
        loc = f"s3://{self.bucket}/{self.prefix}".rstrip("/")
        return f"S3 location {loc}"

    def _is_workflow_key(self, key: str) -> bool:
        if not key.endswith(".py"):
            return False
        name = key[len(self.prefix):]
        # Skip _-prefixed files anywhere in the path.
        return not any(part.startswith("_") for part in name.split("/"))

    def iter_files(self) -> Iterator[WorkflowFile]:
        client = self._client()
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=self.bucket, Prefix=self.prefix
        ):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not self._is_workflow_key(key):
                    continue
                yield self._fetch_key(client, key)

    def fetch(self, name: str) -> WorkflowFile | None:
        client = self._client()
        key = self.prefix + name
        if not self._is_workflow_key(key):
            return None
        try:
            return self._fetch_key(client, key)
        except Exception:  # noqa: BLE001 — a missing/unreadable key → None
            return None

    def _fetch_key(self, client, key: str) -> WorkflowFile:
        body = client.get_object(Bucket=self.bucket, Key=key)["Body"]
        text = body.read().decode("utf-8")
        name = key[len(self.prefix):]
        folder = "/".join(name.split("/")[:-1])
        return WorkflowFile(name=name, folder=folder, source=text)


def workflow_source_from_uri(uri: str) -> WorkflowSource:
    """Build a workflow source from a URI.

      s3://bucket/prefix   -> S3Source
      anything else        -> LocalDirSource (a filesystem path)
    """
    if uri.startswith("s3://"):
        rest = uri[len("s3://"):]
        bucket, _, prefix = rest.partition("/")
        if not bucket:
            raise ValueError(f"malformed S3 URI: {uri!r}")
        return S3Source(bucket=bucket, prefix=prefix)
    return LocalDirSource(Path(uri))
