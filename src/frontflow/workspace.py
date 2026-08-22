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

Navigation is declared the same way, and is itself just a panel:

    @workspace.navigation
    def main_nav():
        return workspace.Nav(
            displays.Markdown("- [Sales ops](/workspaces/sales_ops)"),
            handle=workspace.Handle(icon="\u2630", label="Menu"),
        )

That serves every workspace; any one of them can override it with
`@workspace(nav=...)` or opt out with `nav=None`.

`workspace` is callable (the decorator) *and* carries `Form`, so both
`@workspace(...)` and `workspace.Form(...)` work from one import.
"""

import sys as _sys
from types import ModuleType as _ModuleType

from frontflow.dsl.workspaces import (  # noqa: F401
    DEFAULT_NAVIGATION,
    WORKSPACES,
    Explore,
    Form,
    Handle,
    Nav,
    Navbar,
    Tabs,
    Workspace,
    WorkspaceTemplate,
    compile_workspace,
    navigation,
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
    "DEFAULT_NAVIGATION",
    "Explore",
    "Form",
    "Handle",
    "Nav",
    "Navbar",
    "Tabs",
    "WORKSPACES",
    "Workspace",
    "WorkspaceTemplate",
    "compile_workspace",
    "navigation",
]
