"""`@workspace` — several forms and dashboards on one screen.

A workspace is declared the same way a form is: in Python, in the
workflow source, versioned with it. Its body returns a panel tree.

    from frontflow import displays, workspace

    @workspace(
        workspace_id="sales_ops",
        title="Sales operations",
        private=True,
    )
    def sales_ops():
        return displays.Row(
            workspace.Form("sales"),
            displays.Dashboard("sales_overview"),
        )

    sales_ops()

`displays.Row` and `displays.Column` set the split orientation; the
browser turns the tree into a dockable grid, so panels can be resized,
re-docked, and collapsed from there. The declaration is the starting
arrangement, not a straitjacket.

**Access control lives on the workspace.** That is what makes a
dashboard panel authorizable at all: outside a form there is no form ACL
to borrow, so the workspace's own visibility decides who may see its
dashboards. `private=True` restricts it, mirroring `@form`.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .core import Container, Operator

# workspace_id -> Workspace. Populated by the trailing call on a
# decorated function, mirroring how @form registers into WORKFLOWS.
WORKSPACES: dict[str, "Workspace"] = {}


class Form(Operator):
    """A form, rendered as a panel inside a workspace.

        workspace.Form("sales")

    `form_id` is the same id the form declares. The panel renders that
    form's own filling surface — it is not a copy, so a submission
    started in a workspace is the same submission as anywhere else.

    A workspace grants access to its panels, but a form panel is *also*
    subject to that form's own visibility: putting a restricted form in
    a public workspace does not publish the form.
    """

    kind = "workspace_form"

    def __init__(
        self,
        form_id: str,
        *,
        title: Optional[str] = None,
        id: Optional[str] = None,
    ) -> None:
        if not form_id or not str(form_id).strip():
            raise ValueError(
                'workspace.Form needs a form id, e.g. workspace.Form("sales")'
            )
        super().__init__(id=id or f"form-{str(form_id).strip()}")
        self.form_id = str(form_id).strip()
        self.title = title


class Workspace:
    """A registered workspace: an id, its metadata, and its panel tree."""

    def __init__(
        self,
        *,
        id: str,
        title: str,
        description: str,
        layout: Operator,
        private: bool,
        tags: Optional[list[str]] = None,
    ) -> None:
        self.id = id
        self.title = title
        self.description = description
        self.layout = layout
        self.private = private
        self.tags = tags or []

    def __repr__(self) -> str:
        return f"<Workspace {self.id!r}>"


class WorkspaceTemplate:
    """What `@workspace` produces. Calling it registers the workspace,
    mirroring the trailing `my_form()` call a @form file ends with."""

    def __init__(
        self,
        func: Callable[[], Any],
        *,
        workspace_id: Optional[str],
        title: Optional[str],
        description: Optional[str],
        private: bool,
        tags: Optional[list[str]],
    ) -> None:
        self.func = func
        self.id = workspace_id or func.__name__
        self.title = title or self.id.replace("_", " ").title()
        self.description = description or (func.__doc__ or "").strip()
        self.private = private
        self.tags = tags

    def __call__(self) -> Workspace:
        if self.id in WORKSPACES:
            raise ValueError(
                f"Workspace {self.id!r} is already registered. Each "
                "@workspace must produce a unique workspace_id, and each "
                "template should be called only once."
            )

        layout = self.func()
        if layout is None:
            raise ValueError(
                f"@workspace {self.id!r} returned nothing. A workspace body "
                "must return its panel tree, e.g. "
                "`return displays.Row(workspace.Form('sales'))`."
            )
        if not isinstance(layout, Operator):
            raise TypeError(
                f"@workspace {self.id!r} must return a panel tree "
                f"(containers, workspace.Form, displays.Dashboard); got "
                f"{type(layout).__name__}."
            )

        panels = _collect_panels(layout)
        if not panels:
            raise ValueError(
                f"@workspace {self.id!r} has no panels. Add at least one "
                "workspace.Form(...) or displays.Dashboard(...)."
            )

        ws = Workspace(
            id=self.id,
            title=self.title,
            description=self.description,
            layout=layout,
            private=self.private,
            tags=self.tags,
        )
        WORKSPACES[self.id] = ws
        return ws


def workspace(
    func: Optional[Callable[[], Any]] = None,
    /,
    *,
    workspace_id: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    private: bool = False,
    tags: Optional[list[str]] = None,
) -> Any:
    """Decorate a function as a workspace template.

    Works bare or with arguments, like @form and @node.

    `private=True` restricts the workspace to admins and users granted
    access; the default is public. This is the gate on its dashboards —
    a dashboard panel has no form ACL to inherit, so the workspace's own
    visibility is what authorizes it.
    """

    def wrap(f: Callable[[], Any]) -> WorkspaceTemplate:
        return WorkspaceTemplate(
            f,
            workspace_id=workspace_id,
            title=title,
            description=description,
            private=private,
            tags=tags,
        )

    if func is not None:
        return wrap(func)
    return wrap


# --- compilation -----------------------------------------------------------

# Panel leaves a workspace may contain. Dashboards reuse the display
# block forms already use, so one dashboard means one thing everywhere.
_PANEL_KINDS = ("workspace_form", "dashboard")


def _collect_panels(node: Operator) -> list[Operator]:
    """Every panel leaf in the tree, in declaration order."""
    found: list[Operator] = []
    if getattr(node, "kind", None) in _PANEL_KINDS:
        found.append(node)
    for child in getattr(node, "children", None) or []:
        found.extend(_collect_panels(child))
    return found


def compile_workspace(ws: Workspace) -> dict[str, Any]:
    """The workspace as the JSON the browser renders.

    Same `{type, id, props, children}` shape a form's layout uses, so
    the frontend's recursive renderer needs no second tree format.
    """
    return {
        "workspace_id": ws.id,
        "title": ws.title,
        "description": ws.description,
        "private": ws.private,
        "tags": list(ws.tags),
        "layout": _compile_panel(ws.layout),
    }


def _compile_panel(op: Operator) -> dict[str, Any]:
    kind = getattr(op, "kind", None)

    if kind == "workspace_form":
        return {
            "type": "workspace_form",
            "id": op.id,
            "props": {"form_id": op.form_id, "title": op.title},
            "children": [],
        }

    if kind == "dashboard":
        # Deliberately the same props displays.Dashboard emits inside a
        # form, so one renderer serves both surfaces.
        return {
            "type": "dashboard",
            "id": op.id,
            "props": {
                "name": op.name,
                "connection": op.connection,
                "height": op.height,
                "show_filters": op.show_filters,
            },
            "children": [],
        }

    if isinstance(op, Container):
        return {
            "type": kind,
            "id": op.id,
            "props": {},
            "children": [_compile_panel(c) for c in op.children],
        }

    raise TypeError(
        f"{type(op).__name__} cannot appear in a workspace. A workspace "
        "holds containers (displays.Row / displays.Column), "
        "workspace.Form(...), and displays.Dashboard(...)."
    )
