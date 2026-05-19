"""
DSL core. Defines the base Operator class, layout containers, the
workflow/node decorators, and the definition context.

The DSL has no hidden registration. Every operator — display blocks,
inputs, buttons, backend calls, external tasks — is a **pure value
object**: constructing one has no side effect.

A node becomes "the node's content" purely by what its body **returns**:

    @node
    def review_summary():
        approve = Button("Approve")
        reject  = Button("Reject")

        @backend.branch
        def decide(approve, reject, comments): ...

        finalize = AirflowStatus(...)
        [approve, reject] >> decide(approve, reject, comments) >> finalize

        return displays.Column(
            displays.Markdown("Review the summary…"),
            comments,
            displays.Row(approve, reject),
        )

The returned value is the **layout tree** — display blocks, inputs, and
buttons, nested via container objects. The **execution graph** (@backend
+ external tasks) is wired with `>>` and is reached by walking downstream
from the buttons in that tree. Two structures, no third mechanism:

  - layout      → what the user sees   (the returned tree)
  - execution   → what runs on submit  (the >> graph off the buttons)

Variable names are still captured via sys.settrace, so
`square_footage = inputs.Integer(...)` yields an operator with
id="square_footage" without an explicit input_id.
"""

from __future__ import annotations

import sys
from contextvars import ContextVar
from typing import Any, Callable, Optional


# --- Definition context ----------------------------------------------------

_current_workflow: ContextVar[Optional["Workflow"]] = ContextVar(
    "current_workflow", default=None
)

# True while a @node body is executing. Backend calls created inside a
# node body are node-internal (wired to a button via >>); backend calls
# created at workflow scope become standalone workflow steps.
_building_node: ContextVar[bool] = ContextVar("building_node", default=False)

# The Page currently being built, or None at workflow scope. A @node
# called while this is set registers into that page as one of its
# section nodes; a @node called at workflow scope is a top-level node.
_building_page: ContextVar[Optional["Page"]] = ContextVar(
    "building_page", default=None
)


# --- Base classes ----------------------------------------------------------


class Operator:
    """Base for everything in a node — display blocks, inputs, buttons,
    backend calls, external tasks.

    Pure value object: constructing one has no side effect. An operator
    becomes part of a node by being placed in the tree the node returns,
    or (for execution operators) by being reachable via `>>` from a
    button in that tree.

      - `id`: string identifier, used in JSON / Jinja templates. Either
        set explicitly or assigned from the variable it's bound to
        (captured via sys.settrace at node-build time).
      - `upstream` / `downstream`: execution-graph edges, set via `>>`.
    """

    kind: str = "operator"

    def __init__(self, id: Optional[str] = None) -> None:
        self.id: Optional[str] = id
        self.upstream: list[Operator] = []
        self.downstream: list[Operator] = []

    def __rshift__(self, other: Any) -> Any:
        if isinstance(other, list):
            for op in other:
                self._add_downstream(op)
        else:
            self._add_downstream(other)
        return other

    def __rrshift__(self, other: Any) -> "Operator":
        """`[a, b, c] >> self` — list on the left."""
        if isinstance(other, list):
            for op in other:
                if isinstance(op, Operator):
                    op._add_downstream(self)
        return self

    def _add_downstream(self, other: Any) -> None:
        if not isinstance(other, Operator):
            raise TypeError(
                f"Cannot chain {type(self).__name__} >> {type(other).__name__}; "
                "right-hand side must be an Operator."
            )
        if other not in self.downstream:
            self.downstream.append(other)
        if self not in other.upstream:
            other.upstream.append(self)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} id={self.id!r}>"


class Container(Operator):
    """An operator that holds child operators — a layout container.

    Containers take their children as positional constructor args and
    their own options as keyword args. They form the layout tree:

        displays.Column(
            displays.Markdown("…"),
            displays.Row(field_a, field_b),
        )
    """

    kind = "container"

    def __init__(self, *children: Operator, id: Optional[str] = None) -> None:
        super().__init__(id=id)
        for c in children:
            if not isinstance(c, Operator):
                raise TypeError(
                    f"{type(self).__name__} children must be operators "
                    f"(display blocks, inputs, buttons); got "
                    f"{type(c).__name__}."
                )
        self.children: list[Operator] = list(children)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} children={len(self.children)}>"


# --- Implicit layout -------------------------------------------------------


def _flip(axis: str) -> str:
    return "row" if axis == "column" else "column"


def _normalize_layout(value: Any, axis: str = "column") -> "Operator":
    """Turn a node body's return value into a layout-tree Operator.

    A tuple or list is shorthand for a container: the outermost is a
    Column (vertical), and every level of nesting flips the axis —
    Row, then Column, and so on:

        ((first_name, last_name), submit)
            -> Column[ Row[first_name, last_name], submit ]

    Any DSL element — an input, a Button, a displays.* block, an
    explicit displays.Column/Row/Card/... — is used exactly as given,
    so explicit containers and the tuple shorthand mix freely. `None`
    entries are dropped, so `field if cond else None` works.
    """
    if isinstance(value, Operator):
        return value
    if isinstance(value, (tuple, list)):
        from .displays import Column, Row  # deferred: avoids import cycle

        children = [
            _normalize_layout(child, _flip(axis))
            for child in value
            if child is not None
        ]
        return Column(*children) if axis == "column" else Row(*children)
    raise TypeError(
        f"layout elements must be display elements (inputs.*, "
        f"displays.*, Button, ...) or tuples/lists of them; got "
        f"{type(value).__name__} {value!r}"
    )


# --- Node / Workflow -------------------------------------------------------


class Node:
    """One screen in the workflow.

    `layout` is the operator tree the node body returned — the visual
    content. The execution graph (backend + external tasks) is not
    stored here; the compiler discovers it by walking `>>` downstream
    from the buttons in `layout`.

    A node is either top-level (a step in the workflow directly) or one
    of a page's section nodes — `page` back-references the owning Page
    in the latter case.
    """

    def __init__(
        self,
        id: str,
        *,
        is_landing: bool = False,
        title: Optional[str] = None,
    ) -> None:
        self.id = id
        self.is_landing = is_landing
        # Display title; falls back to a humanized id at compile time.
        self.title = title
        self.layout: Optional[Operator] = None
        self.upstream_nodes: list["Node"] = []
        # The owning page, if this node is a page section node.
        self.page: Optional["Page"] = None
        # Workflow-level execution edges, set by `>>`. The next step(s)
        # after this one. A branch may have several; everything else
        # has zero (terminal) or one.
        self.downstream: list[Any] = []

    def __repr__(self) -> str:
        kind = "Landing" if self.is_landing else "Node"
        return f"<{kind} id={self.id!r}>"


class BackendStep:
    """A workflow-level @backend step. Unlike a node, it has no screen —
    it runs automatically when the workflow reaches it, with the flow
    continuing afterward.

    The values it consumes are passed explicitly at the call site as
    `steps` references — `notify(steps.collect.email, steps.collect)` —
    so its dependencies are visible to the compiler. `args` holds those
    StepRefs, bound positionally to the function's parameters.

    Registered into the workflow's step sequence by call order, exactly
    like a node. `hidden` controls whether it shows in the chain UI.
    """

    def __init__(
        self,
        backend_fn: "BackendFn",
        args: tuple[Any, ...] = (),
        kwargs: Optional[dict[str, Any]] = None,
        *,
        hidden: bool = False,
    ) -> None:
        self.id = backend_fn.name
        self.backend_fn = backend_fn
        self.args = args
        self.kwargs = kwargs or {}
        self.hidden = hidden
        # Workflow-level execution edges, set by `>>` — see Node.downstream.
        self.downstream: list[Any] = []

    def __repr__(self) -> str:
        return f"<BackendStep id={self.id!r}>"


class Page:
    """A page — its own navigated view, and the unit the workflow's `>>`
    graph wires together.

    A page holds a sequence of section nodes (`nodes`), each its own
    screen with its own submit; the user works through them one at a
    time. A *flat* page has a single implicit node built from a layout
    the page body returned directly.

    `is_landing` marks the one landing page — the pre-submission entry.
    """

    def __init__(
        self,
        id: str,
        *,
        is_landing: bool = False,
        title: Optional[str] = None,
    ) -> None:
        self.id = id
        self.is_landing = is_landing
        self.title = title
        # Section nodes, in registration order. Their internal run
        # order follows their own `>>` edges.
        self.nodes: list[Node] = []
        # Whether the page body returned a layout directly (flat page).
        self.is_flat = False
        # Workflow-level execution edges, set by `>>` — see Node.downstream.
        self.downstream: list[Any] = []

    def add_node(self, n: Node) -> None:
        if any(existing.id == n.id for existing in self.nodes):
            raise ValueError(
                f"Duplicate node id {n.id!r} in page {self.id!r}"
            )
        n.page = self
        self.nodes.append(n)

    def entry_node(self) -> Node:
        """The page's first section node — a node marked `@node.landing`,
        else the node with no upstream within the page, else the first
        registered."""
        for n in self.nodes:
            if n.is_landing:
                return n
        in_page = set(self.nodes)
        for n in self.nodes:
            if not any(u in in_page for u in n.upstream_nodes):
                return n
        return self.nodes[0]

    def __repr__(self) -> str:
        kind = "LandingPage" if self.is_landing else "Page"
        return f"<{kind} id={self.id!r} nodes={[n.id for n in self.nodes]}>"


class Workflow:
    """A complete workflow. Built up by the @form decorator.

    `steps` is the ordered execution sequence — Page, Node (a top-level
    single-screen step), and BackendStep, in registration order.
    """

    def __init__(
        self,
        id: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        submission_id_template: Optional[str] = None,
    ) -> None:
        self.id = id
        self.title = title or id
        self.description = description or ""
        self.submission_id_template = submission_id_template
        self.steps: list[Any] = []  # Page | Node | BackendStep, in order

    @property
    def nodes(self) -> list[Node]:
        """Top-level nodes only (not page section nodes), in order."""
        return [s for s in self.steps if isinstance(s, Node)]

    @property
    def pages(self) -> list[Page]:
        return [s for s in self.steps if isinstance(s, Page)]

    def _check_unique(self, step_id: str) -> None:
        if any(s.id == step_id for s in self.steps):
            raise ValueError(
                f"Duplicate step id {step_id!r} in workflow {self.id!r}"
            )

    def _check_one_landing(self, incoming_id: str) -> None:
        if any(getattr(s, "is_landing", False) for s in self.steps):
            raise ValueError(
                f"Workflow {self.id!r} already has a landing step; only "
                f"one @page.landing / @node.landing is allowed "
                f"(adding {incoming_id!r})."
            )

    def add_node(self, n: Node) -> None:
        self._check_unique(n.id)
        if n.is_landing:
            self._check_one_landing(n.id)
        self.steps.append(n)

    def add_page(self, p: Page) -> None:
        self._check_unique(p.id)
        if p.is_landing:
            self._check_one_landing(p.id)
        self.steps.append(p)

    def add_backend_step(self, bs: BackendStep) -> None:
        self._check_unique(bs.id)
        self.steps.append(bs)

    def landing_step(self) -> Any:
        """The workflow's entry step — the one marked `.landing`, else
        the first registered step."""
        for s in self.steps:
            if getattr(s, "is_landing", False):
                return s
        return self.steps[0]

    def landing_node(self) -> Node:
        """The entry node — the first node the user actually sees. For a
        landing Page that's its entry section node."""
        step = self.landing_step()
        return step.entry_node() if isinstance(step, Page) else step

    def __repr__(self) -> str:
        return (
            f"<Workflow id={self.id!r} title={self.title!r} "
            f"steps={[s.id for s in self.steps]}>"
        )


WORKFLOWS: dict[str, Workflow] = {}


# --- Variable-name capture -------------------------------------------------


def _capture_locals(
    func: Callable[..., Any], *args, **kwargs
) -> tuple[Any, dict[str, Any]]:
    """Run `func` and return (its return value, its local variables at
    return time). The locals are used to assign operator ids from the
    variable names they were bound to."""
    captured: dict[str, Any] = {}

    def tracer(frame, event, arg):
        if frame.f_code is not func.__code__:
            return None
        if event == "return":
            captured.update(frame.f_locals)
        return tracer

    old = sys.gettrace()
    sys.settrace(tracer)
    try:
        result = func(*args, **kwargs)
    finally:
        sys.settrace(old)
    return result, captured


# --- @node / @page ---------------------------------------------------------


def _resolve_step_id(explicit: Optional[str], func: Callable[..., Any]) -> str:
    """The id for a @node / @page — the explicit id passed to the
    decorator, else the decorated function's name.

    Ids surface as `steps.<id>` template keys and as URL path segments,
    so an explicit id must be URL-safe."""
    sid = explicit if explicit is not None else func.__name__
    if not sid or not all(c.isalnum() or c in "_-" for c in sid):
        raise ValueError(
            f"step id {sid!r} is invalid — ids must be non-empty and "
            "use only letters, digits, '_' and '-' (they appear in "
            "`steps.<id>` and in URLs)."
        )
    return sid


class NodeTemplate:
    """Result of `@node` / `@node.landing`. Callable to instantiate the
    node — inside a `@page` body it registers as one of that page's
    section nodes; at workflow scope it registers as a top-level node
    (a single-screen workflow step).

    Calling runs the decorated function's body and uses its **return
    value** as the node's layout tree. Returns a NodeRef for chaining.
    """

    def __init__(
        self,
        func: Callable[..., Any],
        *,
        is_landing: bool = False,
        title: Optional[str] = None,
        id: Optional[str] = None,
    ) -> None:
        self.func = func
        self.id = _resolve_step_id(id, func)
        self.is_landing = is_landing
        self.title = title

    def __call__(self, *args: Any, **kwargs: Any) -> "NodeRef":
        n = Node(id=self.id, is_landing=self.is_landing, title=self.title)

        # Data deps: NodeRef args become upstream node edges.
        for a in (*args, *kwargs.values()):
            if isinstance(a, NodeRef) and a.node not in n.upstream_nodes:
                n.upstream_nodes.append(a.node)

        token = _building_node.set(True)
        try:
            layout, captured = _capture_locals(self.func, *args, **kwargs)
        finally:
            _building_node.reset(token)

        # Assign ids to operators from the variable names they're bound to.
        for var_name, value in captured.items():
            if isinstance(value, Operator) and value.id is None:
                value.id = var_name

        if layout is None:
            raise ValueError(
                f"node {self.id!r} returned no layout. A node body must "
                "return its layout tree — an explicit "
                "`displays.Column(...)`, or the tuple shorthand "
                "`return ((a, b), submit)`."
            )
        try:
            n.layout = _normalize_layout(layout)
        except TypeError as e:
            raise TypeError(f"node {self.id!r}: {e}") from e

        page = _building_page.get()
        if page is not None:
            # Inside a @page body — a section node of that page.
            page.add_node(n)
        else:
            wf = _current_workflow.get()
            if wf is not None:
                if self.is_landing and n.upstream_nodes:
                    raise ValueError(
                        f"@node.landing {self.id!r} cannot have upstream "
                        "dependencies — it's a workflow entry point."
                    )
                wf.add_node(n)

        return NodeRef(n)

    def __rshift__(self, other: Any) -> Any:
        raise TypeError(
            f"Node {self.id!r} must be called before chaining with >>. "
            f"Did you mean `{self.id}() >> ...` ?"
        )

    def __repr__(self) -> str:
        kind = "LandingNodeTemplate" if self.is_landing else "NodeTemplate"
        return f"<{kind} {self.id!r}>"


class PageTemplate:
    """Result of `@page` / `@page.landing`. Callable to instantiate the
    page as a workflow step.

    Calling runs the page body. Section nodes (`@node`s) defined in the
    body register into this page. If the body instead *returns* a
    layout tree and defines no section nodes, the page is **flat** — it
    gets a single implicit node carrying that layout.
    """

    def __init__(
        self,
        func: Callable[..., Any],
        *,
        is_landing: bool = False,
        title: Optional[str] = None,
        id: Optional[str] = None,
    ) -> None:
        self.func = func
        self.id = _resolve_step_id(id, func)
        self.is_landing = is_landing
        self.title = title

    def __call__(self, *args: Any, **kwargs: Any) -> "PageRef":
        page = Page(id=self.id, is_landing=self.is_landing, title=self.title)

        page_token = _building_page.set(page)
        try:
            layout, captured = _capture_locals(self.func, *args, **kwargs)
        finally:
            _building_page.reset(page_token)

        if page.nodes:
            # Sectioned page — the body defined @node sections. A return
            # value would be ambiguous, so disallow it.
            if layout is not None:
                raise ValueError(
                    f"page {self.id!r} defines section nodes *and* returns "
                    "a layout. A page either declares @node sections or is "
                    "flat (returns a layout) — not both."
                )
        else:
            # Flat page — the body returned a layout directly. Wrap it in
            # a single implicit node so the rest of the model is uniform.
            if layout is None:
                raise ValueError(
                    f"page {self.id!r} is empty — it must either declare "
                    "@node sections or return a layout (a flat page)."
                )
            for var_name, value in captured.items():
                if isinstance(value, Operator) and value.id is None:
                    value.id = var_name
            implicit = Node(id=self.id, title=self.title)
            try:
                implicit.layout = _normalize_layout(layout)
            except TypeError as e:
                raise TypeError(f"page {self.id!r}: {e}") from e
            page.add_node(implicit)
            page.is_flat = True

        wf = _current_workflow.get()
        if wf is not None:
            wf.add_page(page)

        return PageRef(page)

    def __rshift__(self, other: Any) -> Any:
        raise TypeError(
            f"Page {self.id!r} must be called before chaining with >>. "
            f"Did you mean `{self.id}() >> ...` ?"
        )

    def __repr__(self) -> str:
        kind = "LandingPageTemplate" if self.is_landing else "PageTemplate"
        return f"<{kind} {self.id!r}>"


class _StepRef:
    """Base for the handles `>>` wires together — NodeRef, PageRef,
    BackendStepRef. `>>` between any two declares a workflow execution
    edge; only a @backend.branch may fan out to several.

        landing() >> review() >> route() >> [approve(), reject()]
    """

    _target: Any

    @property
    def id(self) -> str:
        return self._target.id

    @property
    def target(self) -> Any:
        """The underlying Node / Page / BackendStep."""
        return self._target

    def __rshift__(self, other: Any) -> Any:
        if isinstance(other, _StepRef):
            _add_edge(self._target, other._target)
            return other
        if isinstance(other, list):
            for o in other:
                if isinstance(o, _StepRef):
                    _add_edge(self._target, o._target)
            return other
        if isinstance(other, (NodeTemplate, PageTemplate)):
            raise TypeError(
                f"Cannot chain >> uncalled {type(other).__name__} "
                f"{other.id!r}. Did you mean `... >> {other.id}()` ?"
            )
        return NotImplemented

    def __rrshift__(self, other: Any) -> "_StepRef":
        if isinstance(other, list):
            for o in other:
                if isinstance(o, _StepRef):
                    _add_edge(o._target, self._target)
        return self

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.id!r}>"


class NodeRef(_StepRef):
    """Handle for a registered Node, returned by calling a @node."""

    def __init__(self, node: Node) -> None:
        self._target = node

    @property
    def node(self) -> Node:
        return self._target


class PageRef(_StepRef):
    """Handle for a registered Page, returned by calling a @page."""

    def __init__(self, page: Page) -> None:
        self._target = page

    @property
    def page(self) -> Page:
        return self._target


class BackendStepRef(_StepRef):
    """Handle for a registered BackendStep, returned by calling a
    @backend at workflow scope."""

    def __init__(self, step: BackendStep) -> None:
        self._target = step

    @property
    def step(self) -> BackendStep:
        return self._target


def _add_edge(src: Any, dst: Any) -> None:
    """Record a workflow execution edge src → dst, de-duplicated."""
    if dst not in src.downstream:
        src.downstream.append(dst)


class _NodeDecorator:
    """`@node` and `@node.landing`, each usable bare or with arguments:

        @node
        def review(): ...

        @node(title="Review the summary", id="review_step")
        def review(): ...

        @node.landing
        def start(): ...
    """

    def _make(
        self,
        func: Optional[Callable[..., Any]],
        *,
        is_landing: bool,
        title: Optional[str],
        id: Optional[str],
    ) -> Any:
        def deco(fn: Callable[..., Any]) -> NodeTemplate:
            return NodeTemplate(
                fn, is_landing=is_landing, title=title, id=id
            )

        return deco(func) if func is not None else deco

    def __call__(
        self,
        func: Optional[Callable[..., Any]] = None,
        /,
        *,
        title: Optional[str] = None,
        id: Optional[str] = None,
    ) -> Any:
        return self._make(func, is_landing=False, title=title, id=id)

    def landing(
        self,
        func: Optional[Callable[..., Any]] = None,
        /,
        *,
        title: Optional[str] = None,
        id: Optional[str] = None,
    ) -> Any:
        """Mark a node as a workflow entry node (or, inside a page, the
        page's entry node)."""
        return self._make(func, is_landing=True, title=title, id=id)


class _PageDecorator:
    """`@page` and `@page.landing`, each usable bare or with arguments.
    A page body declares `@node` sections, or returns a layout directly
    (a flat page). `id` overrides the page's identifier — the function
    name by default — which is also its URL path segment."""

    def _make(
        self,
        func: Optional[Callable[..., Any]],
        *,
        is_landing: bool,
        title: Optional[str],
        id: Optional[str],
    ) -> Any:
        def deco(fn: Callable[..., Any]) -> PageTemplate:
            return PageTemplate(
                fn, is_landing=is_landing, title=title, id=id
            )

        return deco(func) if func is not None else deco

    def __call__(
        self,
        func: Optional[Callable[..., Any]] = None,
        /,
        *,
        title: Optional[str] = None,
        id: Optional[str] = None,
    ) -> Any:
        return self._make(func, is_landing=False, title=title, id=id)

    def landing(
        self,
        func: Optional[Callable[..., Any]] = None,
        /,
        *,
        title: Optional[str] = None,
        id: Optional[str] = None,
    ) -> Any:
        """Mark a page as the workflow's one landing page — the
        pre-submission entry."""
        return self._make(func, is_landing=True, title=title, id=id)


node = _NodeDecorator()
page = _PageDecorator()


# --- @form -----------------------------------------------------------------


class WorkflowTemplate:
    """Result of `@form(...)`. Callable to instantiate and register the
    workflow. Decoration declares; the trailing call registers."""

    def __init__(
        self,
        func: Callable[[], None],
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        workflow_id: Optional[str] = None,
        submission_id: Optional[str] = None,
    ) -> None:
        self.func = func
        self.id = workflow_id or func.__name__
        # An unset title defaults to the decorated function's name.
        self.title = title or func.__name__
        self.description = description
        self.submission_id_template = submission_id

    def __call__(self) -> "Workflow":
        if self.id in WORKFLOWS:
            raise ValueError(
                f"Workflow {self.id!r} is already registered. Each "
                "@form must produce a unique workflow_id, and each "
                "WorkflowTemplate should be called only once."
            )

        wf = Workflow(
            id=self.id,
            title=self.title,
            description=self.description,
            submission_id_template=self.submission_id_template,
        )
        wf_token = _current_workflow.set(wf)
        try:
            self.func()
        finally:
            _current_workflow.reset(wf_token)

        if not wf.steps:
            raise ValueError(
                f"Workflow {self.id!r} has no steps. "
                "Did you forget to call your @page / @node functions?"
            )

        WORKFLOWS[self.id] = wf
        return wf

    def __repr__(self) -> str:
        return f"<WorkflowTemplate {self.id!r}>"


def form(
    func: Optional[Callable[[], None]] = None,
    /,
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    workflow_id: Optional[str] = None,
    submission_id: Optional[str] = None,
) -> Any:
    """Decorate a function as a workflow template.

    Works bare or with arguments, like @node / @landing / @backend:

        @form
        def my_workflow():
            ...

        @form(
            title="Publish an article",
            workflow_id="publish_article",
            submission_id="{{ steps.start.name | slugify }}",
        )
        def my_workflow():
            ...

        my_workflow()   # trailing call registers it in WORKFLOWS

    Args:
      title: Page headline on the landing URL. Defaults to the
        decorated function's name when unset.
      description: Page subtitle on the landing URL.
      workflow_id: Identifier (defaults to the function name).
      submission_id: Jinja template evaluated at start-submission time.
    """

    def decorator(fn: Callable[[], None]) -> WorkflowTemplate:
        return WorkflowTemplate(
            fn,
            title=title,
            description=description,
            workflow_id=workflow_id,
            submission_id=submission_id,
        )

    # Bare `@form` — `fn` is the decorated function itself.
    if func is not None:
        return decorator(func)
    # `@form(...)` — return the decorator to be applied next.
    return decorator


# --- Sentinel + BackendCall ------------------------------------------------


class _EndSentinel:
    """Returned by @backend.branch to indicate the workflow should end."""

    def __repr__(self) -> str:
        return "<END>"


END = _EndSentinel()


class BackendCall(Operator):
    """A call to a @backend function with operator-bound arguments.

    Written as `trigger_dag_run(name)` in a node body. Lives in the
    execution graph (`>>`), never in the layout tree.
    """

    kind = "backend_call"

    def __init__(
        self,
        backend_fn: "BackendFn",
        args: tuple[Operator, ...],
        kwargs: dict[str, Operator],
    ) -> None:
        super().__init__(id=backend_fn.name)
        self.backend_fn = backend_fn
        self.args = args
        self.kwargs = kwargs

    def __repr__(self) -> str:
        return f"<BackendCall {self.backend_fn.name}>"
