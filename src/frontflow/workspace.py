"""Workspace namespace — `from frontflow import workspace`.

Mirrors `frontflow.airflow`: a module used as a namespace so a workflow
file reads naturally.

    from frontflow import displays, workspace

    @workspace(workspace_id="sales_ops", title="Sales ops", private=True)
    def sales_ops():
        return displays.Row(
            workspace.Form("sales"),
            displays.Dashboard("sales_overview"),
        )

    sales_ops()

`workspace` is callable (the decorator) *and* carries `Form`, so both
`@workspace(...)` and `workspace.Form(...)` work from one import.
"""

import sys as _sys
from types import ModuleType as _ModuleType

from frontflow.dsl.workspaces import (  # noqa: F401
    WORKSPACES,
    Form,
    Workspace,
    WorkspaceTemplate,
    compile_workspace,
    workspace as _workspace_decorator,
)


class _WorkspaceNamespace(_ModuleType):
    """A module that is also the decorator.

    `@workspace(...)` and `workspace.Form(...)` read best from a single
    import, and Python only allows that if the module object itself is
    callable — hence the subclass.
    """

    def __call__(self, *args, **kwargs):
        return _workspace_decorator(*args, **kwargs)


_sys.modules[__name__].__class__ = _WorkspaceNamespace

__all__ = [
    "Form",
    "WORKSPACES",
    "Workspace",
    "WorkspaceTemplate",
    "compile_workspace",
]
