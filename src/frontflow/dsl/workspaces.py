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

# The navigation every workspace gets unless it says otherwise, keyed by
# panel kind ("nav" / "navbar"). Populated by @workspace.navigation.
DEFAULT_NAVIGATION: dict[str, "Nav"] = {}

# Distinguishes "the author said nothing, so use the default" from "the
# author said None, so this workspace has no navigation". A plain None
# default could not tell those apart.
_INHERIT = object()


# How a panel resolves its height in the dock.
#
#   "scroll"  — the panel takes the room the grid gives it and its
#               content scrolls inside. Resizable both ways.
#   "content" — the panel is as tall as it needs to be, and the vertical
#               sash is locked so it cannot be shortened to a sliver.
#               Width is still free, and it still collapses.
FIT_MODES = ("scroll", "content")


def _check_fit(fit: str, where: str) -> str:
    if fit not in FIT_MODES:
        raise ValueError(
            f"{where} fit must be one of {FIT_MODES}; got {fit!r}. "
            '"scroll" lets the content scroll inside the panel; '
            '"content" sizes the panel to its content and locks the '
            "vertical sash."
        )
    return fit


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
        min_height: Optional[int] = None,
        fit: str = "scroll",
        id: Optional[str] = None,
    ) -> None:
        if not form_id or not str(form_id).strip():
            raise ValueError(
                'workspace.Form needs a form id, e.g. workspace.Form("sales")'
            )
        super().__init__(id=id or f"form-{str(form_id).strip()}")
        self.form_id = str(form_id).strip()
        self.title = title
        # Room this panel needs, in a workspace declared scroll=True.
        self.min_height = None if min_height is None else int(min_height)
        # A form's content height is real DOM the browser can measure, so
        # fit="content" needs no declared height — the panel grows to
        # whatever the form actually is.
        self.fit = _check_fit(fit, "workspace.Form")


class Explore(Operator):
    """Superset's Explore — ad-hoc chart building — as a workspace panel.

        workspace.Explore()                              # pick a dataset
        workspace.Explore(dataset="v_frontflow_submissions")

    This is the self-serve surface: a person picks dimensions and
    metrics and builds their own view of the data, rather than reading a
    dashboard someone else composed.

    **It uses the viewer's own Superset login, not frontflow's.** A guest
    token cannot serve Explore — `GuestTokenResourceType` grants
    dashboards only, and Superset rejects modified query payloads from
    guest users — so there is no way to offer Explore to someone without
    a Superset account. The panel says so rather than showing an
    unexplained login screen.

    Consequently frontflow does not gate this beyond opening the
    workspace: whoever can open the workspace sees the panel, and
    Superset decides what they may actually query.
    """

    kind = "superset_explore"

    def __init__(
        self,
        *,
        dataset: Optional[str] = None,
        connection: Optional[str] = None,
        title: Optional[str] = None,
        min_height: Optional[int] = None,
        fit: str = "scroll",
        id: Optional[str] = None,
    ) -> None:
        super().__init__(id=id or ("explore" if not dataset else f"explore-{dataset}"))
        # Room this panel needs, in a workspace declared scroll=True.
        self.min_height = None if min_height is None else int(min_height)
        # Explore is a cross-origin iframe, so there is no content height
        # to measure — fit="content" means the height declared here.
        self.fit = _check_fit(fit, "workspace.Explore")
        if self.fit == "content" and not self.min_height:
            raise ValueError(
                'workspace.Explore(fit="content") also needs min_height. '
                "Explore renders in a cross-origin iframe, whose content "
                "height cannot be measured from this side, so the height "
                "has to be declared."
            )
        # The table or view to open on. None opens Superset's dataset
        # picker instead, which is the right default when a workspace has
        # more than one thing worth exploring.
        self.dataset = dataset
        self.connection = connection
        self.title = title


class Tabs(Container):
    """Panels stacked as tabs in one dock group, rather than side by side.

        workspace.Tabs(
            displays.Dashboard("sales_overview"),
            workspace.Explore(dataset="v_frontflow_submissions"),
        )

    Row and Column split the screen; Tabs shares one region. It is only
    the *starting* arrangement — a tab can be dragged out into its own
    panel at any time, like any other.
    """

    kind = "tabs"


class Handle:
    """How a collapsed nav panel presents its spine.

        workspace.Handle(icon="\u2630", label="Menu", position="start")

    A nav is closed most of the time, so the spine is the part people
    actually see and aim at. This is what lets an author make it look
    like navigation rather than a panel that happens to be shut.
    """

    POSITIONS = ("start", "center", "end")

    def __init__(
        self,
        *,
        label: Optional[str] = None,
        icon: Optional[str] = None,
        position: str = "start",
    ) -> None:
        if position not in self.POSITIONS:
            raise ValueError(
                f"Handle position must be one of {self.POSITIONS}; "
                f"got {position!r}."
            )
        # Where along the collapsed edge the handle sits: at the top of a
        # side nav, the left of a navbar ("start"), the middle, or the
        # far end.
        self.position = position
        self.label = label
        self.icon = icon

    def serialize(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "icon": self.icon,
            "position": self.position,
        }


class Nav(Container):
    """A navigation panel pinned to the side of a workspace.

        workspace.Nav(
            displays.Markdown("### Acme Ops"),
            displays.Markdown("- [Sales](/workspaces/sales_ops)"),
            handle=workspace.Handle(icon="\u2630", label="Menu"),
        )

    A nav is **an ordinary dock panel**, not a separate kind of chrome.
    It docks, resizes, collapses, and is dragged like any other, and it
    holds any display block — there is no nav-specific vocabulary to
    learn, and a nav can carry a KPI or an image as readily as links.

    What distinguishes it is where it starts and how it starts: pinned
    to an edge and, by default, already collapsed, so a workspace opens
    showing its panels with navigation tucked into a spine.
    """

    kind = "nav"

    #: Edges this panel kind may pin to.
    EDGES = ("left", "right")
    DEFAULT_EDGE = "left"
    DEFAULT_SIZE = 260

    def __init__(
        self,
        *children: Operator,
        position: Optional[str] = None,
        size: Optional[int] = None,
        collapsed: bool = True,
        title: Optional[str] = None,
        handle: Optional[Handle] = None,
        id: Optional[str] = None,
    ) -> None:
        super().__init__(*children, id=id or self.kind)

        edge = position or self.DEFAULT_EDGE
        if edge not in self.EDGES:
            raise ValueError(
                f"{type(self).__name__} position must be one of "
                f"{self.EDGES}; got {position!r}. (A navbar pins to "
                f"{Navbar.EDGES}, a nav to {Nav.EDGES}.)"
            )
        self.position = edge

        # Size along the axis it collapses on: width for a side nav,
        # height for a navbar.
        self.size = int(size if size is not None else self.DEFAULT_SIZE)
        if self.size <= 0:
            raise ValueError(
                f"{type(self).__name__} size must be positive; got {size!r}."
            )

        # Pre-closed by default. A workspace is about its panels; the
        # navigation should not spend screen on itself until asked.
        self.collapsed = bool(collapsed)
        self.title = title
        self.handle = handle

    def serialize(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "position": self.position,
            "size": self.size,
            "collapsed": self.collapsed,
            "title": self.title or ("Menu" if self.kind == "nav" else "Navigation"),
            "handle": (self.handle or Handle()).serialize(),
        }


class Navbar(Nav):
    """A navigation panel pinned across the top of a workspace.

        workspace.Navbar(
            displays.Markdown("**Acme** \u00b7 [Docs](https://example.com)"),
            collapsed=False,
        )

    Identical to `workspace.Nav` in every respect but the edge it pins
    to and how much room it takes, so anything true of one is true of
    the other.
    """

    kind = "navbar"

    EDGES = ("top", "bottom")
    DEFAULT_EDGE = "top"
    DEFAULT_SIZE = 88


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
        nav: Optional["Nav"] = None,
        navbar: Optional["Nav"] = None,
        scroll: bool = False,
    ) -> None:
        self.id = id
        self.title = title
        self.description = description
        self.layout = layout
        self.private = private
        self.tags = tags or []
        self.nav = nav
        self.navbar = navbar
        self.scroll = scroll

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
        nav: Any = _INHERIT,
        navbar: Any = _INHERIT,
        scroll: bool = False,
    ) -> None:
        self.func = func
        self.id = workspace_id or func.__name__
        self.title = title or self.id.replace("_", " ").title()
        self.description = description or (func.__doc__ or "").strip()
        self.private = private
        self.tags = tags
        self.nav = nav
        self.navbar = navbar
        self.scroll = scroll

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
            nav=_resolve_navigation(self.nav, "nav", self.id),
            navbar=_resolve_navigation(self.navbar, "navbar", self.id),
            scroll=self.scroll,
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
    nav: Any = _INHERIT,
    navbar: Any = _INHERIT,
    scroll: bool = False,
) -> Any:
    """Decorate a function as a workspace template.

    Works bare or with arguments, like @form and @node.

    `private=True` restricts the workspace to admins and users granted
    access; the default is public. This is the gate on its dashboards —
    a dashboard panel has no form ACL to inherit, so the workspace's own
    visibility is what authorizes it.

    `scroll=True` lets the workspace be taller than the window. Panels
    declaring `min_height` grow the canvas until every one of them fits,
    and the workspace scrolls to reach what is past the fold. Without it
    the grid fills the window exactly, as a dock normally does.

    `nav` / `navbar` override the app-wide navigation declared with
    `@workspace.navigation`. Pass a `workspace.Nav(...)` (or a function
    returning one) for this workspace only, or `None` to give it no
    navigation at all. Omitting them inherits the default, which is what
    makes one declaration serve every workspace.
    """

    def wrap(f: Callable[[], Any]) -> WorkspaceTemplate:
        return WorkspaceTemplate(
            f,
            workspace_id=workspace_id,
            title=title,
            description=description,
            private=private,
            tags=tags,
            nav=nav,
            navbar=navbar,
            scroll=scroll,
        )

    if func is not None:
        return wrap(func)
    return wrap



# --- navigation ------------------------------------------------------------


def navigation(func: Callable[[], Any]) -> Callable[[], Any]:
    """Declare the navigation every workspace gets by default.

        @workspace.navigation
        def main_nav():
            return workspace.Nav(
                displays.Markdown("- [Sales](/workspaces/sales_ops)"),
            )

    The panel kind decides what it becomes: return a `workspace.Nav` and
    it is the side navigation, a `workspace.Navbar` and it is the top
    bar. Declare one of each to have both.

    Any workspace can opt out or substitute its own with
    `@workspace(nav=...)`, so this is a default rather than a mandate.

    Unlike `@workspace` and `@form`, this needs no trailing call. Those
    are templates — a body that runs to produce something — whereas this
    is a declaration, and the tree it returns is the whole of it.
    """
    panel = func()

    if not isinstance(panel, Nav):
        raise TypeError(
            f"@workspace.navigation {func.__name__!r} must return a "
            f"workspace.Nav(...) or workspace.Navbar(...); got "
            f"{type(panel).__name__}."
        )

    existing = DEFAULT_NAVIGATION.get(panel.kind)
    if existing is not None:
        raise ValueError(
            f"A default {panel.kind} is already declared. There can be "
            f"one @workspace.navigation per panel kind — one Nav and one "
            f"Navbar. To vary it per workspace use "
            f"@workspace(workspace_id=..., {panel.kind}=...)."
        )

    DEFAULT_NAVIGATION[panel.kind] = panel
    return func


def _resolve_navigation(declared: Any, kind: str, workspace_id: str) -> Optional[Nav]:
    """What navigation of `kind` this workspace ends up with.

    Accepts the panel itself or a function returning one, so the same
    declaration can be shared by reference or built per workspace.
    """
    if declared is _INHERIT:
        return DEFAULT_NAVIGATION.get(kind)
    if declared is None:
        # Explicitly opted out.
        return None

    panel = declared() if callable(declared) and not isinstance(declared, Nav) else declared

    if not isinstance(panel, Nav):
        raise TypeError(
            f"@workspace {workspace_id!r} passed {kind}={declared!r}, which "
            f"is not a workspace.Nav(...) / workspace.Navbar(...)."
        )
    if panel.kind != kind:
        raise TypeError(
            f"@workspace {workspace_id!r} passed a {panel.kind} as its "
            f"{kind}. A Nav pins to the side and a Navbar to the top; "
            f"pass it as {panel.kind}=... instead."
        )
    return panel


# --- compilation -----------------------------------------------------------

# Panel leaves a workspace may contain. Dashboards reuse the display
# block forms already use, so one dashboard means one thing everywhere.
_PANEL_KINDS = ("workspace_form", "dashboard", "superset_explore")


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
        "scroll": ws.scroll,
        # How tall the grid must be for every panel to get the room it
        # asked for. 0 means nothing asked, so the grid just fills the
        # window as a dock normally does.
        "min_canvas_height": _min_canvas_height(ws.layout) if ws.scroll else 0,
        # Navigation is a panel like any other, so it ships beside the
        # layout rather than inside it: its position is pinned to an edge
        # of the grid, which the panel tree has no way to express.
        "nav": _compile_nav(ws.nav, ws.id),
        "navbar": _compile_nav(ws.navbar, ws.id),
    }


def _min_canvas_height(op: Operator) -> int:
    """How tall the grid must be for every panel to get its `min_height`.

    Stacking adds and splitting does not: panels in a Column sit above
    one another, so their needs sum, while a Row or a Tabs group shares
    one band of height and only its tallest member sets the floor.

    This is computed here rather than in the browser because it is the
    part with real arithmetic in it, and here it can be tested.
    """
    kind = getattr(op, "kind", None)

    if kind in _PANEL_KINDS:
        declared = getattr(op, "min_height", None)
        return int(declared) if declared else 0

    children = getattr(op, "children", None) or []
    if not children:
        return 0

    heights = [_min_canvas_height(child) for child in children]

    # A Column stacks its children; everything else — Row, Tabs — puts
    # them side by side or on top of each other in the same band.
    return sum(heights) if kind == "column" else max(heights)


def _compile_nav(panel: Optional[Nav], workspace_id: str) -> Optional[dict[str, Any]]:
    """A nav panel as JSON: its own settings plus its contents.

    Contents compile through the SAME block compiler a form node uses,
    which is what makes "any display block" true rather than aspiration
    — a KPI in a nav is the identical block it is in a form, rendered by
    the identical component.
    """
    if panel is None:
        return None

    from .compile import _compile_block, _serialize_block

    # The compiler accumulates inputs and buttons as it walks, for the
    # node that owns them. A nav has no node and no submission, so
    # anything landing here is an author error worth naming precisely
    # rather than rendering a field that can never be submitted.
    collected: dict[str, list[Any]] = {"inputs": [], "buttons": []}
    children = [
        _serialize_block(_compile_block(child, collected, f"<{panel.kind}>", []))
        for child in panel.children
    ]

    if collected["inputs"] or collected["buttons"]:
        raise TypeError(
            f"The {panel.kind} of workspace {workspace_id!r} contains form "
            f"inputs or buttons. Navigation is display-only — it belongs to "
            f"the workspace, not to any submission, so there is nothing for a "
            f"field to bind to. Put the input in a workspace.Form(...) panel."
        )

    return {**panel.serialize(), "children": children}


def _compile_panel(op: Operator) -> dict[str, Any]:
    kind = getattr(op, "kind", None)

    if kind == "workspace_form":
        return {
            "type": "workspace_form",
            "id": op.id,
            "props": {
                "form_id": op.form_id,
                "title": op.title,
                "min_height": op.min_height,
                "fit": op.fit,
            },
            "children": [],
        }

    if kind == "superset_explore":
        return {
            "type": "superset_explore",
            "id": op.id,
            "props": {
                "dataset": op.dataset,
                "connection": op.connection,
                "title": op.title,
                "min_height": op.min_height,
                "fit": op.fit,
            },
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
                "min_height": getattr(op, "min_height", None),
                "fit": getattr(op, "fit", "scroll"),
                "show_filters": op.show_filters,
                "filters_expanded": getattr(op, "filters_expanded", False),
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
        "holds containers (displays.Row / displays.Column / "
        "workspace.Tabs), workspace.Form(...), workspace.Explore(...), "
        "and displays.Dashboard(...)."
    )
