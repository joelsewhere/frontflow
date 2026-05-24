"""Regression tests for the package-import surface.

These tests guard against side-effect-laden imports — anything that
reads the environment, opens files, or connects to a database — being
triggered just by `import frontflow`. The CLI loads configuration
from `--env-file` AFTER the package has been imported, so any import
that reads env at module load time will freeze its config to whatever
was set BEFORE the env file was applied. The Variables tool initially
triggered exactly this bug by eager-importing `frontflow.variables`
at the package root, which transitively imported `store`, which read
DB_PATH at module scope and bound DATABASE_URL too early.

Run these in a subprocess so a clean Python interpreter is guaranteed
— in-process import caches make this kind of test useless otherwise.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


PYTHONPATH = str(
    (Path(__file__).parent.parent / "src").resolve()
)


def _run(code: str) -> tuple[int, str, str]:
    """Run `code` in a fresh Python interpreter with the package on
    PYTHONPATH. Returns (returncode, stdout, stderr)."""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": PYTHONPATH, "PATH": ""},
    )
    return result.returncode, result.stdout, result.stderr


class TestPackageImportIsSideEffectFree:
    def test_store_not_imported_by_package_load(self):
        """`import frontflow` must NOT pull in `frontflow.dsl.store`.

        The store module reads DB_PATH at module load and freezes the
        SQLAlchemy URL. If it imports during plain `import frontflow`,
        the CLI's `--env-file` flag silently misses — every DB write
        lands at the default path."""
        code = (
            "import sys; import frontflow; "
            "assert 'frontflow.dsl.store' not in sys.modules, "
            "'store was imported during package load — "
            "this breaks --env-file resolution'"
        )
        rc, out, err = _run(code)
        assert rc == 0, f"stderr:\n{err}"

    def test_variables_not_imported_by_package_load(self):
        """`from frontflow import variables` must work as a lazy
        attribute access, NOT as a side effect of `import frontflow`.
        See test above for why."""
        code = (
            "import sys; import frontflow; "
            "assert 'frontflow.variables' not in sys.modules, "
            "'variables was eager-imported — moves store import "
            "ahead of CLI env loading'"
        )
        rc, out, err = _run(code)
        assert rc == 0, f"stderr:\n{err}"

    def test_db_path_takes_effect_after_package_load(self):
        """The full bug scenario: import the package with no DB_PATH,
        THEN set DB_PATH, THEN import store. DATABASE_URL should
        reflect the late-set value. If it doesn't, --env-file is
        broken."""
        code = (
            "import os, sys\n"
            "for k in ['DB_PATH', 'DATABASE_URL', 'FRONTFLOW_HOME']:\n"
            "    os.environ.pop(k, None)\n"
            "import frontflow\n"
            "os.environ['DB_PATH'] = '/tmp/late-bound-db-path.sqlite'\n"
            "from frontflow.dsl import store\n"
            "expected = 'sqlite:////tmp/late-bound-db-path.sqlite'\n"
            "actual = store.DATABASE_URL\n"
            "assert actual == expected, (\n"
            "    f'DATABASE_URL did not pick up late DB_PATH: "
            "got {actual!r}, expected {expected!r}'\n"
            ")"
        )
        rc, out, err = _run(code)
        assert rc == 0, f"stderr:\n{err}"

    def test_variables_still_importable_explicitly(self):
        """User workflow files do `from frontflow import variables`
        and it should keep working — just not at package load."""
        code = (
            "import os\n"
            "os.environ['DB_PATH'] = '/tmp/regression-variables-import.db'\n"
            "from frontflow import variables\n"
            "assert hasattr(variables, 'get'), "
            "'variables module missing get() helper'"
        )
        rc, out, err = _run(code)
        assert rc == 0, f"stderr:\n{err}"
