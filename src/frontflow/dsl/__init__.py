"""
Public DSL surface. Workflow modules import from here.

Typical workflow file looks like:

    from workflows import form, page, node, inputs, widgets, displays, \
                          Button, backend, AirflowStatus, END

    @form(title="...", description="...")
    def my_workflow():

        @page
        def signup():
            @node
            def account():
                email = inputs.Text(label="...", required=True)
                return (email, Button("Next"))
            @node
            def profile():
                name = inputs.Text(label="...", required=True)
                return (name, Button("Create account"))
            account() >> profile()

        @node
        def step_two():
            ...

        signup() >> step_two()

    my_workflow()
"""

from . import workspaces as _workspaces_mod  # noqa: F401
from .workspaces import WORKSPACES, Workspace, WorkspaceTemplate  # noqa: F401
from .core import (
    END,
    Assign,
    backend_group,
    Node,
    NodeRef,
    NodeTemplate,
    Operator,
    Page,
    PageRef,
    PageTemplate,
    Role,
    Workflow,
    WorkflowTemplate,
    WORKFLOWS,
    form,
    node,
    page,
)
from .actions import Button
from .backend import BackendFn, backend
from .external import (
    AirflowOperator,
    AirflowStatus,
    ExternalTask,
)
from .external_identity import resolve_external_user
from .pickers import users
from .references import steps
from . import inputs, widgets, displays

__all__ = [
    "form",
    "page",
    "node",
    "Role",
    "Assign",
    "Button",
    "backend",
    "backend_group",
    "AirflowStatus",
    "ExternalTask",
    "AirflowOperator",
    "END",
    "steps",
    "resolve_external_user",
    "users",
    # Operator collections (namespaces)
    "inputs",
    "widgets",
    "displays",
    # For tools/inspection
    "Operator",
    "Node",
    "NodeRef",
    "NodeTemplate",
    "Page",
    "PageRef",
    "PageTemplate",
    "Workflow",
    "WorkflowTemplate",
    "WORKFLOWS",
    "Workspace",
    "WorkspaceTemplate",
    "WORKSPACES",
    "BackendFn",
]
