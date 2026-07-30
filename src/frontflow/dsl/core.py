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

# The active `backend_group` block, if any. Workflow-level BackendSteps
# instantiated inside a `with backend_group(...):` body capture it, so
# the chain UI can collapse them into one status node.
_current_backend_group: ContextVar[Optional[dict]] = ContextVar(
    "current_backend_group", default=None
)

# The Page currently being built, or None at workflow scope. A @node
# called while this is set registers into that page as one of its
# section nodes; a @node called at workflow scope is a top-level node.
_building_page: ContextVar[Optional["Page"]] = ContextVar(
    "building_page", default=None
)


# --- Role ------------------------------------------------------------------


class Role:
    """A named permission symbol scoped to one form.

    Declared at module scope in a form file:

        approver = Role("approver")
        monitor = Role("monitor")

    Referenced by Python identity on nodes (`@node(role=approver)`)
    and on inputs (`inputs.Text(label="x", role=approver)`). The
    string passed in becomes the role's identifier — used in URLs,
    admin UI, and audit logs. Must be unique within the form (the
    compiler validates).

    Two roles with the same identifier in different forms are
    different objects; they do not share permissions or assignments.
    """

    def __init__(self, identifier: str) -> None:
        if not isinstance(identifier, str) or not identifier:
            raise ValueError(
                "Role identifier must be a non-empty string"
            )
        # A loose pattern check — roles are surfaced in URLs and
        # JSON payloads; lock down to safely-serializable chars.
        # Letters, digits, dash, underscore, dot.
        import re as _re
        if not _re.match(r"^[A-Za-z][\w.\-]*$", identifier):
            raise ValueError(
                f"Role identifier {identifier!r} must start with a "
                "letter and contain only letters, digits, '-', '_', "
                "or '.'"
            )
        self.identifier = identifier

    def __repr__(self) -> str:
        return f"Role({self.identifier!r})"

    def __hash__(self) -> int:
        # By Python identity. Two Role objects with the same
        # identifier are still distinct.
        return id(self)


# Sentinel distinguishing "no default_role argument was passed" from
# "explicitly passed default_role=None" (which means strict mode: every
# node must declare its own role). The default of "anyone with form
# access" is what `_DEFAULT_ROLE_NOT_SET` represents.
class _DefaultRoleSentinel:
    def __repr__(self) -> str:
        return "<DEFAULT_ROLE_NOT_SET>"


_DEFAULT_ROLE_NOT_SET = _DefaultRoleSentinel()


class RolePermission:
    """Normalized form of a node's `role=` declaration.

    Two parallel lists of Role objects:
      - write_roles: roles permitted to fill inputs and submit
      - read_roles:  roles permitted to view the node's state

    Anyone in write_roles is automatically in read_roles (you
    can't write without reading); the lists are kept separate
    so the runtime distinguishes the two intents.

    Construct via `_normalize_role_arg(...)`, which accepts the
    three DSL shapes (single role, list of roles, verb-mapped
    dict) and produces a uniform RolePermission.
    """

    def __init__(
        self,
        write_roles: "list[Role]",
        read_roles: "list[Role]",
    ) -> None:
        self.write_roles = list(write_roles)
        # Anyone with write also gets read; merge while preserving
        # order, no duplicates.
        merged: list[Role] = list(read_roles)
        for r in write_roles:
            if r not in merged:
                merged.append(r)
        self.read_roles = merged

    def __repr__(self) -> str:
        return (
            f"RolePermission(write={[r.identifier for r in self.write_roles]}, "
            f"read={[r.identifier for r in self.read_roles]})"
        )


def _normalize_role_arg(value: Any, *, context: str) -> RolePermission:
    """Parse `role=` argument into a RolePermission.

    Accepts:
      - A single Role          → write only, no extra read roles
      - A list/tuple of Roles  → all are write roles
      - A dict {"write": ..., "read": ...} where each value is
        a Role or a list of Roles

    Raises ValueError with `context` describing where the bad
    declaration was found (e.g., "node 'review'").
    """
    if isinstance(value, Role):
        return RolePermission(write_roles=[value], read_roles=[])
    if isinstance(value, (list, tuple)):
        for item in value:
            if not isinstance(item, Role):
                raise ValueError(
                    f"{context}: role list must contain Role "
                    f"objects, got {type(item).__name__}: {item!r}"
                )
        return RolePermission(write_roles=list(value), read_roles=[])
    if isinstance(value, dict):
        valid_keys = {"write", "read"}
        bad = set(value) - valid_keys
        if bad:
            raise ValueError(
                f"{context}: unknown role verb(s) {sorted(bad)!r}; "
                f"only 'write' and 'read' are supported"
            )

        def _to_list(v: Any, *, verb: str) -> list[Role]:
            if v is None:
                return []
            if isinstance(v, Role):
                return [v]
            if isinstance(v, (list, tuple)):
                for item in v:
                    if not isinstance(item, Role):
                        raise ValueError(
                            f"{context}: '{verb}' role list must "
                            f"contain Role objects, got "
                            f"{type(item).__name__}: {item!r}"
                        )
                return list(v)
            raise ValueError(
                f"{context}: '{verb}' must be a Role or list of "
                f"Roles, got {type(v).__name__}: {v!r}"
            )

        write = _to_list(value.get("write"), verb="write")
        read = _to_list(value.get("read"), verb="read")
        if not write and not read:
            raise ValueError(
                f"{context}: role dict must specify at least one of "
                "'write' or 'read'"
            )
        return RolePermission(write_roles=write, read_roles=read)
    raise ValueError(
        f"{context}: role= must be a Role, a list of Roles, or a "
        f"dict {{'write': ..., 'read': ...}}; got "
        f"{type(value).__name__}: {value!r}"
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
        # Per-input write-role gate. None = no per-input gate (the
        # node-level role decides). When set to a Role, only users
        # with that role can write this specific input. Set
        # post-construction via the input's `role=` kwarg in shapes
        # that support it (`inputs.Text(..., role=approver)`); the
        # base Operator carries the field so any input type inherits
        # it without per-class plumbing. Read is governed at the
        # node level — never per input (see design doc §1.1).
        self.role: Optional[Role] = None

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
        title: Optional[str] = None,
        role: "Optional[RolePermission]" = None,
    ) -> None:
        self.id = id
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
        # Per-node permission declaration. `None` means "no role
        # gate" — the default role applies (any user with form-level
        # access can read + write). When set, it's a RolePermission
        # struct carrying write_roles and read_roles. See
        # `_normalize_role_arg` for the parsing.
        self.role: Optional["RolePermission"] = role

    def __repr__(self) -> str:
        return f"<Node id={self.id!r}>"


class backend_group:
    """Group workflow-level `@backend` steps under one chain-UI status
    node — the Airflow TaskGroup idiom:

        with backend_group("Building report"):
            transform_step = transform(...)
            kpi_step = kpi_totals(...)

    Purely presentational: execution order, wiring, and the edit
    cascade are untouched. The UI collapses consecutive steps of a
    group into a single node titled with the group's title, showing
    the currently running sub-step and, expanded, every sub-step's
    own status. `group_id` defaults to a slug of the title.
    """

    def __init__(self, title: str, *, group_id: Optional[str] = None) -> None:
        self.title = title
        gid = group_id or "".join(
            ch if ch.isalnum() else "_" for ch in title.lower()
        ).strip("_")
        if not gid:
            raise ValueError("backend_group needs a non-empty title/group_id")
        self.id = gid
        self._token: Any = None

    def __enter__(self) -> "backend_group":
        self._token = _current_backend_group.set(
            {"id": self.id, "title": self.title}
        )
        return self

    def __exit__(self, *exc: Any) -> None:
        _current_backend_group.reset(self._token)


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
        # The enclosing `with backend_group(...):`, if any — a
        # {"id", "title"} dict the compiler carries to the chain UI.
        self.group = _current_backend_group.get()
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
    """

    def __init__(
        self,
        id: str,
        *,
        title: Optional[str] = None,
    ) -> None:
        self.id = id
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
        """The page's first section node — the one with no upstream
        within the page, else the first registered."""
        in_page = set(self.nodes)
        for n in self.nodes:
            if not any(u in in_page for u in n.upstream_nodes):
                return n
        return self.nodes[0]

    def __repr__(self) -> str:
        return f"<Page id={self.id!r} nodes={[n.id for n in self.nodes]}>"


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
        reports: Optional[dict[str, Any]] = None,
        tags: Optional[list[str]] = None,
        iframe_allowed_origins: Optional[list[str]] = None,
        private: bool = False,
        auto_repin_minor: Optional[bool] = None,
        background_backends: bool = False,
        role: "Optional[RolePermission]" = None,
        default_role: Any = _DEFAULT_ROLE_NOT_SET,
        on_assigned: Optional[Callable[[Any], None]] = None,
        on_submitted: Optional[Callable[[Any], None]] = None,
        on_failed: Optional[Callable[[Any], None]] = None,
        on_revoked: Optional[Callable[[Any], None]] = None,
    ) -> None:
        self.id = id
        self.title = title or id
        self.description = description or ""
        self.submission_id_template = submission_id_template
        # Per-form analytics config — author overrides only. Validated
        # lazily at the analytics endpoint, not at workflow build, so
        # adding new recognized keys doesn't require touching this file.
        self.reports = reports or {}
        # Display-only labels — what features or concepts the form
        # demonstrates. Shown as a column on the forms listing so an
        # operator can scan a directory and see at a glance what each
        # form covers ("airflow", "variables", "branching", …). Free
        # strings; no enum or registry. Workflow authors pick the
        # vocabulary that fits their install.
        self.tags = list(tags) if tags else []
        # Origins permitted to embed this form in an iframe.
        # `None` (default) = embedding disallowed; `[]` is equivalent.
        # Each entry is an origin string with explicit scheme:
        #   `https://company.com`       — exact match
        #   `https://*.company.com`     — subdomain glob (any depth,
        #                                 the bare `company.com` is NOT
        #                                 implicitly included)
        #   `*`                         — any origin (only use for
        #                                 genuinely public marketing
        #                                 forms; this disables the
        #                                 main security boundary)
        # The list is emitted directly as a CSP `frame-ancestors`
        # directive value, so the browser is the enforcement point.
        # Only `public` forms are actually iframable; non-public forms
        # with this set get logged and served `frame-ancestors 'none'`.
        self.iframe_allowed_origins = (
            list(iframe_allowed_origins)
            if iframe_allowed_origins
            else None
        )
        # Initial visibility — applied only when the Form row is first
        # created. `True` maps to "restricted" (admin/ACL-only); `False`
        # (the default) maps to "public". After first creation, the
        # admin owns this value via the visibility API. The DSL field
        # describes the form's *initial* policy at the moment the
        # workflow file is first discovered. Three-way visibility
        # (public/unlisted/restricted) remains the underlying model;
        # `private` is sugar over the two extremes since `unlisted` is
        # operationally an admin-facing toggle (mint token, share
        # link).
        self.private = bool(private)
        # Per-form override of the env-default auto-repin-on-minor-
        # bump behavior. None = silent (use the env var
        # FRONTFLOW_AUTO_REPIN_MINOR). True / False = force the
        # behavior on this form regardless of env. See
        # `_should_auto_repin_minor` in main.py.
        self.auto_repin_minor: Optional[bool] = (
            None if auto_repin_minor is None else bool(auto_repin_minor)
        )
        # Run workflow-level @backend steps in a background worker so
        # requests return immediately and the chain UI shows each step
        # completing (see runtime.advance). Opt-in — synchronous
        # execution remains the default contract.
        self.background_backends = bool(background_backends)
        # Form-level role= declaration that nodes inherit when they
        # don't declare their own `role=`. Pre-normalized by the
        # template at decoration time, so by the time we hold a
        # Workflow this is either None or a RolePermission instance.
        # Node-level role= overrides this completely (no merging
        # — explicit-on-node wins, top-to-bottom).
        self.role: "Optional[RolePermission]" = role
        # default_role controls what happens when a node has no
        # `role=` declaration AND the form has no `role=` either:
        #   _DEFAULT_ROLE_NOT_SET (the default) → "open mode"
        #     — anyone with form-level access can read + write.
        #     Backward-compatible with forms that don't use roles.
        #   None → strict mode. A node without `role=` is a
        #     compile-time error.
        # When the form HAS a `role=`, the form-level role becomes
        # the effective default and `default_role` only matters if
        # set to `None` (which still demands every node declare
        # explicit role= rather than inherit).
        self.default_role: Any = default_role
        # Per-form notification hook called when an Assign operator
        # on this form grants a new assignment. Receives an event
        # dict (see runtime._fire_on_assigned_hook for the shape).
        # Hook failures are logged + swallowed; they do NOT roll
        # back the persisted grant (design doc §6.3).
        self.on_assigned: Optional[Callable[[Any], None]] = on_assigned
        # Submission lifecycle hooks. Each receives an event dict;
        # failures are logged + swallowed (design doc §6.3).
        #   on_submitted — fires when a submission reaches a
        #     terminal SUCCESS state (the runtime's `terminated`
        #     flag flips true and `failed` stays false).
        #   on_failed — fires when a submission reaches a terminal
        #     FAILED state (a backend raised, a chain step errored).
        #   on_revoked — fires when an assignment on this form is
        #     revoked (admin action, external system, edit cascade).
        # Each is per-form; project-wide defaults use a customer
        # @form wrapper (design doc §6.4).
        self.on_submitted: Optional[Callable[[Any], None]] = on_submitted
        self.on_failed: Optional[Callable[[Any], None]] = on_failed
        self.on_revoked: Optional[Callable[[Any], None]] = on_revoked
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

    def add_node(self, n: Node) -> None:
        self._check_unique(n.id)
        # The workflow's first step is the entry — it can't have
        # upstream dependencies, because there's nothing upstream.
        if not self.steps and n.upstream_nodes:
            raise ValueError(
                f"entry node {n.id!r} cannot have upstream "
                "dependencies — it's the workflow's first step. "
                "Either remove the upstream reference or declare a "
                "different first node."
            )
        self.steps.append(n)

    def add_page(self, p: Page) -> None:
        self._check_unique(p.id)
        self.steps.append(p)

    def add_backend_step(self, bs: BackendStep) -> None:
        self._check_unique(bs.id)
        self.steps.append(bs)

    def landing_step(self) -> Any:
        """The workflow's entry step — the first registered step."""
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
        title: Optional[str] = None,
        id: Optional[str] = None,
        role: Any = None,
    ) -> None:
        self.func = func
        self.id = _resolve_step_id(id, func)
        self.title = title
        # Pre-normalize at decoration time so a bad role declaration
        # surfaces at workflow load, not at request time.
        if role is not None:
            self._role = _normalize_role_arg(
                role, context=f"node {self.id!r}"
            )
        else:
            self._role = None

    def __call__(self, *args: Any, **kwargs: Any) -> "NodeRef":
        n = Node(id=self.id, title=self.title, role=self._role)

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
                wf.add_node(n)

        return NodeRef(n)

    def __rshift__(self, other: Any) -> Any:
        raise TypeError(
            f"Node {self.id!r} must be called before chaining with >>. "
            f"Did you mean `{self.id}() >> ...` ?"
        )

    def __repr__(self) -> str:
        return f"<NodeTemplate {self.id!r}>"


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
        title: Optional[str] = None,
        id: Optional[str] = None,
    ) -> None:
        self.func = func
        self.id = _resolve_step_id(id, func)
        self.title = title

    def __call__(self, *args: Any, **kwargs: Any) -> "PageRef":
        page = Page(id=self.id, title=self.title)

        # Set BOTH context flags during the body. `_building_page`
        # lets nested `@node` sections register themselves into this
        # page. `_building_node` lets `@backend.__call__` recognize
        # itself as node-internal (chain backend wired to a Button)
        # rather than workflow-scope (standalone step taking
        # `steps.<x>` refs).
        #
        # Why both: a flat @page (body returns a layout, no sections)
        # is conceptually a single-node screen. Without `_building_node`
        # set here, putting a `@backend.branch` or `@backend` inside
        # the page body would trip `@backend.__call__`'s workflow-scope
        # guard and reject the Button arg. Setting both flags makes
        # flat pages support chain wiring identically to @node bodies.
        # For sectioned pages, the inner @node decorators set their
        # OWN `_building_node` (idempotent via the token stack), so
        # sections still work as before. A chain backend created at
        # the page-body level of a sectioned page is captured but
        # not attached to any section's chain — the user would notice
        # it never runs.
        page_token = _building_page.set(page)
        node_token = _building_node.set(True)
        try:
            layout, captured = _capture_locals(self.func, *args, **kwargs)
        finally:
            _building_node.reset(node_token)
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
        return f"<PageTemplate {self.id!r}>"


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
    """`@node`, usable bare or with arguments:

        @node
        def review(): ...

        @node(title="Review the summary", id="review_step")
        def review(): ...

    The workflow's entry node is whichever is registered first.
    """

    def _make(
        self,
        func: Optional[Callable[..., Any]],
        *,
        title: Optional[str],
        id: Optional[str],
        role: Any,
    ) -> Any:
        def deco(fn: Callable[..., Any]) -> NodeTemplate:
            return NodeTemplate(fn, title=title, id=id, role=role)

        return deco(func) if func is not None else deco

    def __call__(
        self,
        func: Optional[Callable[..., Any]] = None,
        /,
        *,
        title: Optional[str] = None,
        id: Optional[str] = None,
        role: Any = None,
    ) -> Any:
        return self._make(func, title=title, id=id, role=role)


class _PageDecorator:
    """`@page`, usable bare or with arguments. A page body declares
    `@node` sections, or returns a layout directly (a flat page). `id`
    overrides the page's identifier — the function name by default —
    which is also its URL path segment. The workflow's entry page is
    whichever is registered first."""

    def _make(
        self,
        func: Optional[Callable[..., Any]],
        *,
        title: Optional[str],
        id: Optional[str],
    ) -> Any:
        def deco(fn: Callable[..., Any]) -> PageTemplate:
            return PageTemplate(fn, title=title, id=id)

        return deco(func) if func is not None else deco

    def __call__(
        self,
        func: Optional[Callable[..., Any]] = None,
        /,
        *,
        title: Optional[str] = None,
        id: Optional[str] = None,
    ) -> Any:
        return self._make(func, title=title, id=id)


node = _NodeDecorator()
page = _PageDecorator()


# --- @form -----------------------------------------------------------------


def _validate_iframe_origin(entry: str, *, form_id: str) -> None:
    """Sanity-check a single `iframe_allowed_origins` entry.

    Browser CSP `frame-ancestors` matching is the security boundary;
    we don't try to fully parse the URL here. We just guard against
    the most common author mistakes:
      - non-string entry
      - empty string
      - bare hostname with no scheme (`company.com` instead of
        `https://company.com`) — CSP would silently not match and
        the author would be debugging a "why isn't this embedding"
        with no visible error
      - trailing path / query / fragment (origin only)

    Raises ValueError with the form_id in the message so the source
    is obvious in load_errors.
    """
    if not isinstance(entry, str):
        raise ValueError(
            f"Workflow {form_id!r}: iframe_allowed_origins entries "
            f"must be strings, got {type(entry).__name__}: {entry!r}"
        )
    if not entry:
        raise ValueError(
            f"Workflow {form_id!r}: iframe_allowed_origins entry is "
            "an empty string"
        )
    if entry == "*":
        # Any-origin wildcard — explicit opt-in to "no restriction"
        # (CSP `frame-ancestors *`). Allowed as a single entry.
        return
    if "://" not in entry:
        raise ValueError(
            f"Workflow {form_id!r}: iframe_allowed_origins entry "
            f"{entry!r} is missing a scheme. Use e.g. "
            f"'https://{entry}' or 'https://*.{entry}' for subdomain "
            "glob, or '*' to allow any origin."
        )
    # Origin only — no path, no query, no fragment. After the
    # scheme://host, nothing should follow except maybe a port.
    scheme, _, rest = entry.partition("://")
    if "/" in rest or "?" in rest or "#" in rest:
        raise ValueError(
            f"Workflow {form_id!r}: iframe_allowed_origins entry "
            f"{entry!r} should be an origin (scheme://host[:port]), "
            "not a full URL."
        )


class WorkflowTemplate:
    """Result of `@form(...)`. Callable to instantiate and register the
    workflow. Decoration declares; the trailing call registers."""

    def __init__(
        self,
        func: Callable[[], None],
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        form_id: Optional[str] = None,
        submission_id: Optional[str] = None,
        reports: Optional[dict[str, Any]] = None,
        tags: Optional[list[str]] = None,
        iframe_allowed_origins: Optional[list[str]] = None,
        private: bool = False,
        auto_repin_minor: Optional[bool] = None,
        background_backends: bool = False,
        role: Any = None,
        default_role: Any = _DEFAULT_ROLE_NOT_SET,
        on_assigned: Optional[Callable[[Any], None]] = None,
        on_submitted: Optional[Callable[[Any], None]] = None,
        on_failed: Optional[Callable[[Any], None]] = None,
        on_revoked: Optional[Callable[[Any], None]] = None,
    ) -> None:
        self.func = func
        self.background_backends = bool(background_backends)
        self.id = form_id or func.__name__
        # An unset title defaults to the decorated function's name.
        self.title = title or func.__name__
        self.description = description
        self.submission_id_template = submission_id
        self.reports = reports
        self.tags = list(tags) if tags else []
        # Validate iframe_allowed_origins at decoration time so a
        # malformed entry surfaces at workflow load, not at request
        # time. The check is intentionally lightweight — verify each
        # entry is a string with an explicit scheme (or the bare `*`
        # any-origin wildcard); leave finer URL parsing to the
        # browser, which is the security boundary.
        if iframe_allowed_origins is not None:
            for entry in iframe_allowed_origins:
                _validate_iframe_origin(entry, form_id=self.id)
            self.iframe_allowed_origins = list(iframe_allowed_origins)
        else:
            self.iframe_allowed_origins = None
        self.private = bool(private)
        # Per-form override of the env-default auto-repin behavior.
        # See Workflow.auto_repin_minor for semantics.
        self.auto_repin_minor: Optional[bool] = (
            None if auto_repin_minor is None else bool(auto_repin_minor)
        )
        # Normalize the form-level role= declaration once at template
        # creation time so authoring errors (bad shape, non-Role
        # objects) surface at import time, not at compile time. None
        # means "no form-level default" — node-level role= still
        # works as before.
        self.role = (
            _normalize_role_arg(role, context=f"form {self.id!r}")
            if role is not None
            else None
        )
        # default_role is tri-valued:
        #   _DEFAULT_ROLE_NOT_SET → open mode
        #   None                  → strict mode
        #   any other value       → a role specification (Role / list /
        #     dict) that's the inherited default for nodes without
        #     their own role=. Normalized once here so authoring
        #     errors fire at import time. Distinct from `role=`:
        #     `role=` is the form-level role declaration AND its
        #     inheritance default; `default_role=` is JUST the
        #     inheritance default — useful when an author wants a
        #     default without committing the form itself to a role.
        #     When both are set, `default_role` wins for inheritance
        #     (the explicit override).
        if (
            default_role is _DEFAULT_ROLE_NOT_SET
            or default_role is None
        ):
            self.default_role = default_role
        else:
            self.default_role = _normalize_role_arg(
                default_role,
                context=f"form {self.id!r} default_role",
            )
        self.on_assigned = on_assigned
        self.on_submitted = on_submitted
        self.on_failed = on_failed
        self.on_revoked = on_revoked

    def __call__(self) -> "Workflow":
        if self.id in WORKFLOWS:
            raise ValueError(
                f"Workflow {self.id!r} is already registered. Each "
                "@form must produce a unique form_id, and each "
                "WorkflowTemplate should be called only once."
            )

        wf = Workflow(
            id=self.id,
            title=self.title,
            description=self.description,
            submission_id_template=self.submission_id_template,
            reports=self.reports,
            tags=self.tags,
            iframe_allowed_origins=self.iframe_allowed_origins,
            private=self.private,
            auto_repin_minor=self.auto_repin_minor,
            background_backends=self.background_backends,
            role=self.role,
            default_role=self.default_role,
            on_assigned=self.on_assigned,
            on_submitted=self.on_submitted,
            on_failed=self.on_failed,
            on_revoked=self.on_revoked,
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
    form_id: Optional[str] = None,
    submission_id: Optional[str] = None,
    reports: Optional[dict[str, Any]] = None,
    tags: Optional[list[str]] = None,
    iframe_allowed_origins: Optional[list[str]] = None,
    private: bool = False,
    auto_repin_minor: Optional[bool] = None,
    background_backends: bool = False,
    role: Any = None,
    default_role: Any = _DEFAULT_ROLE_NOT_SET,
    on_assigned: Optional[Callable[[Any], None]] = None,
    on_submitted: Optional[Callable[[Any], None]] = None,
    on_failed: Optional[Callable[[Any], None]] = None,
    on_revoked: Optional[Callable[[Any], None]] = None,
) -> Any:
    """Decorate a function as a workflow template.

    Works bare or with arguments, like @node / @landing / @backend:

        @form
        def my_workflow():
            ...

        @form(
            title="Publish an article",
            form_id="publish_article",
            submission_id="{{ steps.start.name | slugify }}",
            tags=["airflow", "hitl"],
        )
        def my_workflow():
            ...

        my_workflow()   # trailing call registers it in WORKFLOWS

    Args:
      title: Page headline on the landing URL. Defaults to the
        decorated function's name when unset.
      description: Page subtitle on the landing URL.
      form_id: Identifier (defaults to the function name).
      submission_id: Jinja template evaluated at start-submission time.
      reports: Optional per-form analytics config. Authors only set
        this to override the defaults — most forms leave it unset.
        Recognized keys (all optional):
          - `default_filters`: dict of default filter values applied
            when the analytics page loads without query params.
            Sub-keys: `date_range` (one of "all_time", "last_7_days",
            "last_30_days", "last_90_days"; default "last_30_days"),
            `state` (list of state names, None = all), `current_step`
            (list of node ids, None = all).
      tags: Display-only labels surfaced as a column on the forms
        listing. Free strings; pick whatever vocabulary fits the
        install ("airflow", "branching", "internal", "customer-facing").
        Useful for scanning a directory of forms at a glance.
      iframe_allowed_origins: Origins permitted to embed this form
        in an iframe on a third-party page. `None` (default) =
        embedding disallowed; the form responds with
        `frame-ancestors 'none'` and any iframe attempt is blocked
        by the browser. When set, each entry is an origin string:

            iframe_allowed_origins=[
                "https://company.com",        # exact match
                "https://*.company.com",      # subdomain glob
            ]

        `"*"` allows any origin (use only for genuinely public
        marketing forms). Subdomain glob requires explicit scheme.
        Only `public` forms are actually iframable — non-public
        forms (`unlisted`, `restricted`) with this set will be
        served with `frame-ancestors 'none'` regardless, with a
        warning logged on each request.
      private: Restrict the form to admins and explicitly-permitted
        users on first discovery. Sugar for an initial visibility
        of `restricted` (the existing three-way model — `public`,
        `unlisted`, `restricted` — is still the underlying truth).
        Applies ONLY when the form is first registered; after that,
        the admin owns visibility via the API and re-scans don't
        revert their choice. Default `False` (the form is public
        on first discovery, same as before this flag existed).
      default_role: Controls fallback behavior when a node has no
        explicit `role=` declaration:
          - Unset (the default) → nodes without `role=` are
            fillable by anyone with form-level access. Backward-
            compatible with forms that don't use roles at all.
          - `None` → strict mode. Every node MUST declare its
            `role=`; a node without one is a compile-time error.
            Use this for forms where every action should be
            explicitly role-gated.
      on_assigned: Optional callable invoked after an Assign
        operator on this form grants a new assignment. Receives
        an event dict with keys: kind, parent_form_id,
        parent_submission_handle, child_form_id,
        child_submission_handle, assignee_user_id,
        assignee_username, role_id, assignment_id. Hook failures
        are logged + swallowed (design doc §6.3) — they do not
        roll back the grant. Use this to send notifications
        (Slack, email) that lead the assignee to the child form.
    """

    def decorator(fn: Callable[[], None]) -> WorkflowTemplate:
        return WorkflowTemplate(
            fn,
            title=title,
            description=description,
            form_id=form_id,
            submission_id=submission_id,
            reports=reports,
            tags=tags,
            iframe_allowed_origins=iframe_allowed_origins,
            private=private,
            auto_repin_minor=auto_repin_minor,
            background_backends=background_backends,
            role=role,
            default_role=default_role,
            on_assigned=on_assigned,
            on_submitted=on_submitted,
            on_failed=on_failed,
            on_revoked=on_revoked,
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


class Assign(Operator):
    """Create per-submission role assignments on another form.

    Lives in the execution graph (`>>`), like @backend calls — chained
    from a button on the current node. When the operator fires, it:

      1. Resolves `to` to one or more concrete users using the
         picker's identifier_kind (user_id → direct, external_id →
         hook, email → match-or-create, group_id → expand to members).
      2. Finds or creates a child submission of `form`, pinned to the
         current form_version, prefilled with `prefill`. The child
         carries `parent_submission_handle` + the originating Assign's
         node + op-index for the parent-child UI.
      3. Inserts a submission_assignment row (user, role, child) via
         `assignments.grant()` — idempotent re-grants are no-ops.
      4. Triggers the form's `on_assigned` notification hook
         (Phase 5; for Phase 4, the hook is called if registered).

    Compile-time validation (in `compile.py`):
      - `form` exists in the workflow registry.
      - `to` references a PickerInput on the current node.
      - `role` matches an identifier in the child form's
        permission_template["roles"].
      - Free-text inputs as `to=` are rejected with an actionable
        error pointing the author to `@users.email`.

    Usage:

        @users(label="Recruiter")
        def recruiter(ctx): ...

        @node
        def kickoff():
            project = inputs.Text(label="Project")
            submit = Button("Kick off")
            spawn = Assign(
                form="hiring_screening",
                to=steps.kickoff.recruiter,
                role="recruiter",
                prefill={"project": steps.kickoff.project},
            )
            submit >> spawn
            return project, recruiter, submit

    Args:
      form: form_id of the child form. Validated at compile time.
      to: a StepRef pointing to a PickerInput on the current node.
        Free-text inputs are rejected at compile time.
      role: identifier string of a role declared on the child form.
      prefill: dict mapping input id → value. Values can be literals,
        step references, or Jinja templates (same engine as
        `displays.Markdown`).
      link_ttl_days: lifetime of the signed link sent to the
        assignee (Phase 5; default 7).
    """

    kind = "assign"

    def __init__(
        self,
        *,
        form: str,
        to: Any,
        role: str,
        prefill: Optional[dict[str, Any]] = None,
        link_ttl_days: int = 7,
        id: Optional[str] = None,
    ) -> None:
        super().__init__(id=id)
        if not isinstance(form, str) or not form:
            raise ValueError(
                "Assign(form=...) must be a non-empty form_id string"
            )
        if not isinstance(role, str) or not role:
            raise ValueError(
                "Assign(role=...) must be a non-empty role identifier"
            )
        if not isinstance(link_ttl_days, int) or link_ttl_days <= 0:
            raise ValueError(
                "Assign(link_ttl_days=...) must be a positive integer"
            )
        self.form_id = form
        self.to_ref = to
        self.role_id = role
        self.prefill = dict(prefill) if prefill else {}
        self.link_ttl_days = link_ttl_days

    def __repr__(self) -> str:
        return (
            f"<Assign form={self.form_id!r} role={self.role_id!r}>"
        )
