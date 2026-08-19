"""`frontflow.Assets` — sibling files resolve identically whether a form
was served from a local directory or from S3."""
from __future__ import annotations

from pathlib import Path

import pytest

from frontflow import Assets
from frontflow.dsl.sources import LocalDirSource, S3Source


# --- local origins ---------------------------------------------------------


def test_local_reads_sibling(tmp_path):
    form = tmp_path / "admin" / "my_form.py"
    form.parent.mkdir(parents=True)
    form.write_text("# form")
    (form.parent / "sql").mkdir()
    (form.parent / "sql" / "q.sql").write_text("SELECT 1;")

    a = Assets(str(form))
    assert not a.is_s3
    assert a.read_text("sql/q.sql") == "SELECT 1;"
    assert a.exists("sql/q.sql")
    assert not a.exists("sql/nope.sql")


def test_local_resolution_is_cwd_independent(tmp_path, monkeypatch):
    """The whole point of anchoring on the origin: the server's working
    directory must not matter."""
    form = tmp_path / "f.py"
    form.write_text("# form")
    (tmp_path / "sql").mkdir()
    (tmp_path / "sql" / "q.sql").write_text("SELECT 2;")

    a = Assets(str(form))
    monkeypatch.chdir(tmp_path.parent)
    assert a.read_text("sql/q.sql") == "SELECT 2;"


def test_escape_and_absolute_paths_rejected(tmp_path):
    a = Assets(str(tmp_path / "f.py"))
    with pytest.raises(ValueError):
        a.read_text("../secrets.env")
    with pytest.raises(ValueError):
        a.read_text("/etc/passwd")


def test_cache_and_clear(tmp_path):
    form = tmp_path / "f.py"
    form.write_text("# form")
    asset = tmp_path / "a.sql"
    asset.write_text("one")

    a = Assets(str(form))
    assert a.read_text("a.sql") == "one"
    asset.write_text("two")
    assert a.read_text("a.sql") == "one"  # served from cache
    a.clear_cache()
    assert a.read_text("a.sql") == "two"


# --- S3 origins ------------------------------------------------------------


class _FakeHook:
    def __init__(self, objects):
        self.objects = objects
        self.calls = []

    def read_bytes(self, *, bucket, key):
        self.calls.append((bucket, key))
        try:
            return self.objects[(bucket, key)]
        except KeyError:
            raise FileNotFoundError(key) from None


def test_s3_reads_sibling_key(monkeypatch):
    hook = _FakeHook(
        {("bkt", "forms/planning/sql/q.sql"): b"SELECT 3;"},
    )
    a = Assets("s3://bkt/forms/planning/my_form.py")
    monkeypatch.setattr(a, "_s3_hook", lambda: hook)

    assert a.is_s3
    assert a.read_text("sql/q.sql") == "SELECT 3;"
    assert hook.calls == [("bkt", "forms/planning/sql/q.sql")]
    # Cached — no second fetch.
    a.read_text("sql/q.sql")
    assert len(hook.calls) == 1
    assert not a.exists("sql/missing.sql")


def test_s3_locate():
    a = Assets("s3://bkt/forms/planning/my_form.py")
    assert a.locate("sql/q.sql") == "s3://bkt/forms/planning/sql/q.sql"


# --- sources stamp the origin ---------------------------------------------


def test_local_source_sets_uri(tmp_path):
    (tmp_path / "f.py").write_text("# form")
    wf = next(iter(LocalDirSource(tmp_path).iter_files()))
    assert wf.uri == str((tmp_path / "f.py").resolve())
    # An Assets built from it points back at the same directory.
    assert Assets(wf.uri).locate("sql/x.sql").endswith("sql/x.sql")


def test_s3_source_sets_uri():
    src = S3Source(bucket="bkt", prefix="forms/planning")
    wf = src._fetch_key(
        type("C", (), {
            "get_object": staticmethod(
                lambda **kw: {"Body": type("B", (), {
                    "read": staticmethod(lambda: b"# form")
                })()}
            )
        })(),
        "forms/planning/my_form.py",
    )
    assert wf.uri == "s3://bkt/forms/planning/my_form.py"
    assert wf.name == "my_form.py"
