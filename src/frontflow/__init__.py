"""frontflow — author a stateful, multi-step web form as a Python file.

A workflow file declares a form with a small decorator DSL; frontflow
serves it, captures submissions, and routes users through the chain. A
form can drive an Airflow DAG through connected operators.

The authoring API is re-exported here, so a workflow file is just:

    from frontflow import form, node, inputs, displays, Button

Run a directory of workflow files with the CLI:

    frontflow serve ./my-workflows
"""

# Re-export the full authoring DSL so user workflow files import from
# the package root: `from frontflow import form, node, inputs, ...`.
#
# IMPORTANT: keep this file free of side-effect-laden submodule imports.
# In particular do NOT `from frontflow import variables` here — that
# triggers `frontflow.dsl.store` import, which reads DB_PATH from env
# at module load. Eager-importing it would freeze the DB path before
# the CLI's `--env-file` has a chance to populate the environment, and
# every CLI command would silently write to the default ~/.frontflow
# location regardless of what the env file says.
#
# Submodules like `airflow` and `variables` stay reachable as
# `frontflow.airflow` / `frontflow.variables` via normal Python
# submodule lookup — user workflow files that do `from frontflow
# import variables` resolve correctly because by that point the CLI
# has already loaded the env file and store imports see DB_PATH.
from frontflow.dsl import *  # noqa: F401,F403
from frontflow.dsl import __all__ as _dsl_all
from frontflow import airflow  # noqa: F401 — namespace for connected Airflow operators

__all__ = list(_dsl_all) + ["airflow", "variables"]
__version__ = "1.0.0"
