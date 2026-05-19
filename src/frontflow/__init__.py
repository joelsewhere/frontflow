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
from frontflow.dsl import *  # noqa: F401,F403
from frontflow.dsl import __all__ as _dsl_all

__all__ = list(_dsl_all)
__version__ = "1.0.0"
