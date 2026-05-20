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

from .core import (
    END,
    Node,
    NodeRef,
    NodeTemplate,
    Operator,
    Page,
    PageRef,
    PageTemplate,
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
    AirflowDagSensor,
    AirflowHitl,
    AirflowHitlBranch,
    AirflowOperator,
    AirflowStatus,
    AirflowTaskSensor,
    ExternalTask,
    TriggerDag,
    XComPull,
)
from .references import steps
from . import inputs, widgets, displays

__all__ = [
    "form",
    "page",
    "node",
    "Button",
    "backend",
    "AirflowStatus",
    "ExternalTask",
    "AirflowOperator",
    "TriggerDag",
    "AirflowTaskSensor",
    "AirflowDagSensor",
    "XComPull",
    "AirflowHitl",
    "AirflowHitlBranch",
    "END",
    "steps",
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
    "BackendFn",
]
