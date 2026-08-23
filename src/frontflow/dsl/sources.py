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

    `path` is the file's ABSOLUTE filesystem path when the source is
    local, else None (S3 objects have no local path).

    `uri` is the file's CANONICAL ORIGIN, defined for every source: an
    absolute filesystem path for local files, an `s3://bucket/key` URI
    for S3 objects. The executor uses it as the module's `__file__`,
    so `frontflow.Assets(__file__)` can resolve sibling assets (a
    `sql/` directory next to the form, say) no matter where the form
    was served from — local directory or object store.
    """

    name: str
    folder: str
    source: str
    path: Path | None = None
    uri: str | None = None


@dataclass(frozen=True)
class SourceAsset:
    """A non-executed file carried alongside the workflows.

    Deliberately NOT a WorkflowFile: nothing here is ever exec'd. A
    rendered story is HTML that goes straight to a browser, and a `.xmd`
    is read only to check whether that HTML is out of date. Keeping the
    two types apart means "is this executed?" is answered by the type
    rather than by remembering which suffix it came from.
    """

    name: str
    folder: str
    text: str
    uri: str | None = None


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

    def iter_assets(self, suffixes: tuple[str, ...]) -> Iterator[SourceAsset]:
        """Yield every non-executed file whose name ends in one of
        `suffixes` — data stories and their rendered HTML.

        Sources that hold only Python return nothing.
        """
        return iter(())


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
            # Skip `_`-prefixed files (convention: internal) and any
            # file under a `_`-prefixed directory (e.g. `_seeds/` from
            # `frontflow example install`). `__pycache__` is excluded
            # by name too; it's already covered by the `_` rule, kept
            # for clarity.
            if (
                path.name.startswith("_")
                or any(p.startswith("_") for p in path.relative_to(self.directory).parts)
            ):
                continue
            rel = path.relative_to(self.directory)
            rel_dir = str(rel.parent)
            folder = "" if rel_dir == "." else rel_dir
            yield WorkflowFile(
                name=str(rel),
                folder=folder,
                source=path.read_text(encoding="utf-8"),
                path=path,
                uri=str(path),
            )


    def iter_assets(self, suffixes: tuple[str, ...]) -> Iterator[SourceAsset]:
        if not self.directory.is_dir():
            return
        for path in sorted(self.directory.rglob("*")):
            if not path.is_file() or not path.name.endswith(suffixes):
                continue
            rel = path.relative_to(self.directory)
            if any(part.startswith("_") for part in rel.parts):
                continue
            rel_dir = str(rel.parent)
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                # An asset that is not text is not a story; skip it
                # rather than failing the whole scan.
                continue
            yield SourceAsset(
                name=str(rel),
                folder="" if rel_dir == "." else rel_dir,
                text=text,
                uri=str(path),
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

    def iter_assets(self, suffixes: tuple[str, ...]) -> Iterator[SourceAsset]:
        client = self._client()
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith(suffixes):
                    continue
                name = key[len(self.prefix):]
                if any(part.startswith("_") for part in name.split("/")):
                    continue
                body = client.get_object(Bucket=self.bucket, Key=key)["Body"]
                try:
                    text = body.read().decode("utf-8")
                except UnicodeDecodeError:
                    continue
                yield SourceAsset(
                    name=name,
                    folder="/".join(name.split("/")[:-1]),
                    text=text,
                    uri=f"s3://{self.bucket}/{key}",
                )

    def _fetch_key(self, client, key: str) -> WorkflowFile:
        body = client.get_object(Bucket=self.bucket, Key=key)["Body"]
        text = body.read().decode("utf-8")
        name = key[len(self.prefix):]
        folder = "/".join(name.split("/")[:-1])
        return WorkflowFile(
            name=name,
            folder=folder,
            source=text,
            uri=f"s3://{self.bucket}/{key}",
        )


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
