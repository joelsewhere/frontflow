"""
Compile-time: convert a Workflow (built by the DSL) into structured
runtime data the FastAPI runtime can serve.

Each node is compiled from two structures:

  - its **layout tree** — the operator tree the node body returned.
    Compiled into a nested CompiledBlock tree: containers with children,
    leaves for display blocks / inputs / buttons. This is what the
    frontend renders.

  - its **execution graph** — discovered by walking `>>` downstream from
    the buttons in the layout tree: at most one @backend call, then a
    chain of ExternalTask operators.

The compiler also extracts flat `fields` and `buttons` lists from the
tree — the runtime needs them for argument-binding and submit handling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .actions import Button
from .backend import BackendFn
from .core import (
    Assign,
    BackendCall,
    BackendStep,
    Container,
    Node,
    Operator,
    Page,
    RolePermission,
    Workflow,
)
from .displays import Callout, Card, Cell, Collapsible, Comments, Dashboard, Divider, Figure, Grid, Image, KPI, KPIGroups, Markdown, S3Download, Section, Table
from .conditions import When
from .external import (
    DagSensor,
    Hitl,
    HitlBranch,
    HitlResponse,
    AirflowStatus,
    TaskSensor,
    TaskStateSensor,
    ExternalTask,
    TriggerDag,
    XComPull,
)
from .inputs import ChoiceInput, Input
from .references import STEP_REF_RE, TEMPLATED_PROPS, StepRef
from .widgets import Categorizer, DistributionFilter, RedistributionEditor


# --- Compiled structures ---------------------------------------------------


@dataclass
class CompiledBlock:
    """One node in the compiled layout tree. Shipped to the frontend as
    `{type, id, props, children}` and rendered by a recursive component
    registry — display blocks, inputs, buttons, and containers all use
    this uniform shape (Dash-style)."""
    type: str
    id: Optional[str] = None
    props: dict[str, Any] = field(default_factory=dict)
    children: list["CompiledBlock"] = field(default_factory=list)


@dataclass
class CompiledField:
    """A form field — an input or widget extracted from the layout tree.
    The runtime uses these for required-field validation and for binding
    submitted values to @backend arguments.

    `conditions` is the cumulative visibility condition stamped from the
    enclosing `When` blocks (a conjunction — all must hold for the field
    to be shown). Empty for an unconditional field. Each entry is the
    serialized `{field, op, value}` form."""
    name: str
    label: str
    type: str  # "text" | "number" | "select" | "textarea" | "widget" | ...
    required: bool
    options: list[str] = field(default_factory=list)
    conditions: list[dict[str, Any]] = field(default_factory=list)
    # For file-upload fields (type "file" / "s3file") — the server-side
    # upload config: max size, accepted extensions, and (s3file only)
    # the target bucket. None for non-file fields. The bucket is here
    # rather than in the layout props because it must not be exposed to
    # the browser.
    file_spec: Optional[dict[str, Any]] = None
    # Per-input write-role gate, if `role=` was set on the input.
    # None = no per-input gate (the node's role decides). When set,
    # the value is the role's identifier string (matching the role
    # declared in the form's permission template). Used by the
    # runtime auth check to decide whether the current user may
    # submit this specific input. Read is governed at the node
    # level; never per input.
    role: Optional[str] = None
    # For picker inputs (`type == "picker"`) — what kind of identifier
    # the picker produces. None for non-picker fields. The runtime's
    # Assign-execution path reads this to decide how to resolve picked
    # values into user_ids:
    #   "frontflow_user_id" → direct DB lookup
    #   "external_id"       → resolve_external_user hook
    #   "email"             → match-or-create stub user
    #   "frontflow_group_id"→ expand to group members
    identifier_kind: Optional[str] = None


@dataclass
class CompiledButton:
    """One submit-style button extracted from the layout tree."""
    id: str
    label: str
    # Whether clicking this button closes the node. None follows the
    # node's own `closes`.
    advances: Optional[bool] = None


@dataclass
class CompiledBackendCall:
    """The @backend / @backend.branch invocation downstream of the buttons."""
    fn: BackendFn
    arg_op_ids: list[str]
    # True when any arg references a chain step's output (an operator
    # or an earlier backend) — the call then waits for those upstream
    # steps to finish before running, so it executes inside the chain
    # processor rather than synchronously at submit time. False when
    # every arg is a form field or button, in which case it can still
    # run at submit (preserving today's behavior for forms with no
    # operator chain).
    defers_to_chain: bool = False


@dataclass
class CompiledExternalTask:
    """Compiled form of an ExternalTask operator."""
    task_id: str
    kind: str
    config: dict[str, Any]
    # Whether this task is a graph node — set by the operator class.
    graph_visible: bool = False
    # Omit this operator from the chain UI. Distinct from
    # `graph_visible`, which is about the workflow GRAPH and defaults
    # False for most operators — keying visibility off that would hide
    # nearly every operator that exists.
    hidden: bool = False
    # Whether a user may rerun this operator on its own from the UI.
    retryable: bool = True
    # How often the frontend should poll this operator while in-flight,
    # in milliseconds. None → fall back to the framework default
    # (see useSubmission). Surfaced separately from `config` because
    # this is purely frontend UX; the runtime never reads it.
    poll_interval_ms: int | None = None


@dataclass
class CompiledAssign:
    """Compiled form of an `Assign` operator — present in the
    execution chain. Carries every value the runtime needs to
    perform the assignment (resolve `to`, find-or-create child
    submission, insert submission_assignment row, fire on_assigned).

    `to_ref_descriptor` is the serialized `steps.<node>.<input>`
    reference the operator was constructed with. Resolved at
    runtime against the parent submission's data.

    `prefill_descriptors` carries prefill values — each entry is
    either a literal or a serialized step-reference descriptor.

    `op_idx` is the per-node ordinal of this Assign — when a node
    has multiple Assigns to the same child form / role, this
    distinguishes them and keys the child submission's
    parent_assign_op_idx column.
    """
    form_id: str
    role_id: str
    to_ref_descriptor: dict[str, Any]
    prefill_descriptors: dict[str, Any]
    link_ttl_days: int
    op_idx: int


@dataclass
class CompiledChainStep:
    """One step in a node's `>>` execution chain.

    Exactly one of `external_task` or `backend_call` is set — the
    `kind` field marks which. The chain walks these in declared
    order; each step's args reference earlier steps' outputs (form
    fields, button states, prior operator outputs, prior backend
    returns), and a step runs once all its dependencies are
    available.
    """
    kind: str  # "external_task" | "backend_call"
    external_task: Optional[CompiledExternalTask] = None
    backend_call: Optional[CompiledBackendCall] = None

    @property
    def step_id(self) -> str:
        """The id the chain step exposes as `steps.<step_id>` — the
        operator's task_id, or the backend function's name."""
        if self.external_task is not None:
            return self.external_task.task_id
        assert self.backend_call is not None
        return self.backend_call.fn.name


@dataclass
class CompiledNode:
    id: str
    # Display title — humanized id when none was given.
    title: str
    # The layout tree the frontend renders.
    layout: CompiledBlock
    # Flat extracts from the tree, for the runtime.
    fields: list[CompiledField]
    buttons: list[CompiledButton]
    # The execution graph, walked from the buttons via `>>`.
    # `chain` is the canonical representation — operators and backends
    # in declared order. The legacy `backend_call` and `external_tasks`
    # fields are kept as derived views for read-side code that hasn't
    # been migrated to walk the chain directly yet; they are the
    # backends and external tasks the chain contains, respectively.
    # Whether submitting this node closes it and advances the chain.
    # False makes it a control panel, submitted over and over.
    closes: bool = True
    chain: list[CompiledChainStep] = field(default_factory=list)
    backend_call: Optional[CompiledBackendCall] = None
    external_tasks: list[CompiledExternalTask] = field(default_factory=list)
    # Assign operators reachable via `>>` from this node's buttons.
    # Each carries the child form_id, role, picker reference,
    # prefill, and the per-node op-idx. Empty for nodes without
    # Assign — most nodes today.
    assigns: list[CompiledAssign] = field(default_factory=list)
    # Execution edges (downstream step ids), from `>>`. For a top-level
    # node these are workflow-level; for a page section node they are
    # page-internal (to sibling section nodes).
    downstream: list[str] = field(default_factory=list)
    # Static `steps.<node>.<field>` dependencies — every upstream value
    # this node's layout reads, tagged functional/display. Drives the
    # dependency-aware cascade when an upstream node is edited.
    deps: list["StepDep"] = field(default_factory=list)
    # Per-node role permissions, if `role=` was set on `@node(...)`.
    # When None, the form's default role applies (anyone with form-
    # level access). When set, it's a dict-shaped declaration:
    #   {"write": ["approver"], "read": ["monitor", "approver"]}
    # where both lists carry role IDENTIFIERS (strings) — the
    # CompiledWorkflow's permission_template carries the full Role
    # objects for the form. Anyone in write is auto-included in read.
    role: Optional[dict[str, list[str]]] = None


@dataclass
class CompiledBackendStep:
    """A workflow-level backend step — runs automatically when reached,
    no screen. `fn` carries the function; `arg_refs` / `kwarg_refs` are
    the `steps` references its arguments name, resolved against the
    submission's data and bound to the function's parameters when it
    runs. `hidden` controls whether it appears in the chain UI."""
    id: str
    fn: BackendFn
    is_branch: bool
    hidden: bool
    # Presentational grouping from `with backend_group(...):` — the
    # chain UI collapses consecutive steps sharing a group_id into one
    # status node titled group_title. None = ungrouped.
    group_id: Optional[str] = None
    group_title: Optional[str] = None
    # `steps` references the call passed as arguments — each a
    # serialized {node, name} descriptor (name None = whole-node).
    arg_refs: list[dict[str, Any]] = field(default_factory=list)
    kwarg_refs: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Workflow-level execution edges (downstream step ids), from `>>`.
    downstream: list[str] = field(default_factory=list)


@dataclass
class CompiledPage:
    """A page — a workflow step, and its own navigated view. Holds the
    section nodes the user works through one at a time.

    `nodes` are compiled section nodes; their `downstream` holds the
    page-internal `>>` edges. `entry_node_id` is the first section node
    the user sees; `terminal_node_ids` are the section nodes with no
    internal downstream — submitting one of those ends the page and
    the workflow advances via the page's own `downstream`.
    """
    id: str
    is_flat: bool
    title: str
    nodes: list[CompiledNode]
    entry_node_id: str
    terminal_node_ids: list[str]
    # Workflow-level execution edges (downstream step ids), from `>>`.
    downstream: list[str] = field(default_factory=list)

    @property
    def nodes_by_id(self) -> dict[str, CompiledNode]:
        return {n.id: n for n in self.nodes}


@dataclass
class CompiledWorkflow:
    id: str
    title: str
    # The execution sequence — CompiledPage, CompiledNode (a top-level
    # single-screen step), and CompiledBackendStep, in registration
    # order. Run order follows each step's `downstream` edges.
    steps: list[Any]
    description: str = ""
    submission_id_template: Optional[str] = None
    by_id: dict[str, Any] = field(default_factory=dict)
    # Every CompiledNode by id — top-level nodes AND page section nodes.
    all_nodes_by_id: dict[str, CompiledNode] = field(default_factory=dict)
    # Section node id -> the CompiledPage that owns it.
    node_page: dict[str, CompiledPage] = field(default_factory=dict)
    # Per-form analytics config from `@form(reports=...)`. An empty
    # dict means "use the framework defaults" — last_30_days date
    # range, no state/current_step filter pre-applied.
    reports: dict[str, Any] = field(default_factory=dict)
    # Display-only labels from `@form(tags=...)`. Surfaced on the
    # forms listing so operators can scan a directory and see at a
    # glance what each form demonstrates. Empty list when the author
    # didn't tag the form. Stored on the compiled workflow + the
    # form_version snapshot so the listing reads them with no
    # additional join.
    tags: list[str] = field(default_factory=list)
    # Origins permitted to embed this form in an iframe, from
    # `@form(iframe_allowed_origins=...)`. `None` (default) means
    # embedding is disallowed and the server emits
    # `frame-ancestors 'none'`. Entries are origin strings — see
    # `Workflow.iframe_allowed_origins` for syntax. Threaded through
    # the form_version snapshot so version pins stay self-describing.
    iframe_allowed_origins: Optional[list[str]] = None
    # Permission template — declared in DSL via `Role` symbols,
    # `@node(role=...)`, and per-input `role=`. Snapshot lives on
    # the form_version row so historical access decisions remain
    # auditable.
    #
    # Shape:
    #   {
    #     "roles": ["approver", "monitor"],   # identifiers, declaration order
    #     "default_role_mode": "open" | "strict",
    #         # "open"   → nodes without role= are fillable by anyone
    #         #            with form-level access (backward-compatible)
    #         # "strict" → @form(default_role=None) was set; every
    #         #            node must declare role= (validated at compile)
    #   }
    # Empty roles + "open" mode is the default — equivalent to "no
    # role gates declared", indistinguishable from a form that
    # doesn't use the role system at all. The runtime short-circuits
    # to the existing visibility-only check in this case.
    permission_template: dict[str, Any] = field(default_factory=lambda: {
        "roles": [], "default_role_mode": "open",
    })

    def __post_init__(self) -> None:
        self.by_id = {s.id: s for s in self.steps}
        self.all_nodes_by_id = {}
        self.node_page = {}
        for s in self.steps:
            if isinstance(s, CompiledNode):
                self.all_nodes_by_id[s.id] = s
            elif isinstance(s, CompiledPage):
                for sn in s.nodes:
                    self.all_nodes_by_id[sn.id] = sn
                    self.node_page[sn.id] = s

    @property
    def nodes(self) -> list[CompiledNode]:
        """Top-level single-screen nodes only (not page section nodes)."""
        return [s for s in self.steps if isinstance(s, CompiledNode)]

    @property
    def pages(self) -> list[CompiledPage]:
        return [s for s in self.steps if isinstance(s, CompiledPage)]

    def landing_step(self) -> Any:
        """The workflow's entry step — the first registered step."""
        return self.steps[0]

    def landing_node(self) -> CompiledNode:
        """The entry node the user first sees — the entry page's entry
        section node, or the entry node itself."""
        step = self.landing_step()
        if isinstance(step, CompiledPage):
            return step.nodes_by_id[step.entry_node_id]
        return step


# --- Serialization ---------------------------------------------------------
#
# A CompiledWorkflow holds Python callables (@backend / @backend.branch
# functions) which can't be JSON-serialized. `serialize_workflow` captures
# only the *structure* — layouts, fields, buttons, edges, page/node shape —
# which is everything needed to render and analyze a submission. Executable
# behavior is reconstructed separately, by recompiling the stored DSL source.


def _serialize_block(b: "CompiledBlock") -> dict[str, Any]:
    return {
        "type": b.type,
        "id": b.id,
        "props": b.props,
        "children": [_serialize_block(c) for c in b.children],
    }


def _serialize_node(n: "CompiledNode") -> dict[str, Any]:
    return {
        "step_kind": "node",
        "id": n.id,
        "title": n.title,
        # Per-node role permissions, if declared. None when the node
        # has no role= and the default applies.
        "role": (dict(n.role) if n.role is not None else None),
        "closes": n.closes,
        "layout": _serialize_block(n.layout),
        "fields": [
            {
                "name": f.name,
                "label": f.label,
                "type": f.type,
                "required": f.required,
                "options": f.options,
                # Per-input write-role gate. Identifier string;
                # null when the input has no role= and the node's
                # role decides.
                "role": f.role,
            }
            for f in n.fields
        ],
        "buttons": [
            {"id": b.id, "label": b.label, "advances": b.advances}
            for b in n.buttons
        ],
        # The execution graph's callables are dropped; only the presence
        # of a backend call and the external-task shape are kept.
        "has_backend_call": n.backend_call is not None,
        "backend_call_name": (
            n.backend_call.fn.name if n.backend_call is not None else None
        ),
        "external_tasks": [
            {
                "task_id": t.task_id,
                "kind": t.kind,
                "config": t.config,
                "graph_visible": t.graph_visible,
                # Serialized, or `hidden=True` survives compilation and
                # is then lost the moment the graph is persisted — the
                # step reappears in the chain UI on the next load, which
                # is exactly what the author asked it not to do.
                "hidden": t.hidden,
                "retryable": t.retryable,
            }
            for t in n.external_tasks
        ],
        "downstream": list(n.downstream),
    }


def _serialize_step(s: Any) -> dict[str, Any]:
    if isinstance(s, CompiledPage):
        return {
            "step_kind": "page",
            "id": s.id,
            "is_flat": s.is_flat,
            "title": s.title,
            "nodes": [_serialize_node(n) for n in s.nodes],
            "entry_node_id": s.entry_node_id,
            "terminal_node_ids": list(s.terminal_node_ids),
            "downstream": list(s.downstream),
        }
    if isinstance(s, CompiledBackendStep):
        return {
            "step_kind": "backend",
            "id": s.id,
            "fn_name": s.fn.name,
            "is_branch": s.is_branch,
            "hidden": s.hidden,
            "retryable": s.fn.retryable,
            "downstream": list(s.downstream),
        }
    return _serialize_node(s)


def serialize_workflow(cw: CompiledWorkflow) -> dict[str, Any]:
    """A JSON-able snapshot of a compiled workflow's structure — the
    form_version.compiled_graph payload. Render-ready; carries no Python."""
    return {
        "id": cw.id,
        "title": cw.title,
        "description": cw.description,
        "submission_id_template": cw.submission_id_template,
        "tags": list(cw.tags),
        "iframe_allowed_origins": (
            list(cw.iframe_allowed_origins)
            if cw.iframe_allowed_origins is not None
            else None
        ),
        # Permission template — versioned with the form_version
        # snapshot so historical permission state is recoverable.
        # See `_build_permission_template` for shape.
        "permission_template": dict(cw.permission_template),
        "steps": [_serialize_step(s) for s in cw.steps],
    }


def workflow_content_hash(serialized: dict[str, Any]) -> str:
    """A stable hash of a serialized workflow — identifies a distinct
    compiled state, so form_version rows are created only on real change."""
    import hashlib
    import json

    canonical = json.dumps(serialized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- Compiler --------------------------------------------------------------


def compile_workflow(wf: Workflow) -> CompiledWorkflow:
    compiled: list[Any] = []
    # Pick the form-level inheritance default for nodes without their
    # own role=. When `default_role=` was passed a RolePermission, it
    # wins (it's the more specific kwarg); otherwise fall back to
    # `role=`. The sentinel `_DEFAULT_ROLE_NOT_SET` and explicit
    # `None` for default_role both leave the inheritance falling
    # through to `role=` (or to no inheritance at all, signalling
    # open / strict at the permission_template level).
    form_default_role = (
        wf.default_role
        if isinstance(wf.default_role, RolePermission)
        else wf.role
    )
    for s in wf.steps:
        if isinstance(s, Page):
            compiled.append(
                _compile_page(s, form_default_role=form_default_role)
            )
        elif isinstance(s, Node):
            compiled.append(
                _compile_node(s, form_default_role=form_default_role)
            )
        elif isinstance(s, BackendStep):
            compiled.append(_compile_backend_step(s))
        else:
            raise TypeError(
                f"Unknown workflow step type {type(s).__name__!r} in "
                f"workflow {wf.id!r}"
            )
    cw = CompiledWorkflow(
        id=wf.id,
        title=wf.title,
        description=wf.description,
        steps=compiled,
        submission_id_template=wf.submission_id_template,
        reports=getattr(wf, "reports", None) or {},
        tags=list(getattr(wf, "tags", None) or []),
        iframe_allowed_origins=(
            list(wf.iframe_allowed_origins)
            if getattr(wf, "iframe_allowed_origins", None) is not None
            else None
        ),
    )
    _build_permission_template(wf, cw)
    _validate_edges(cw)
    _validate_node_buttons(cw)
    _validate_step_refs(cw)
    _validate_backend_step_args(cw)
    _validate_figure_data_refs(cw)
    return cw


def _validate_figure_data_refs(cw: CompiledWorkflow) -> None:
    """Validate Figure blocks' `data_from` descriptors against the
    full step set.

    Two acceptable shapes for the descriptor:
      - `{node: <node_id>, name: <backend_fn>}` — a node-internal
        @backend that returns bytes. The runtime resolves via
        `steps.<node>.<backend_fn>`.
      - `{node: <step_id>, name: None}` — a whole-node ref pointing
        at a *workflow-level* @backend step (its `steps.<step_id>`
        namespace IS its return value).

    The inline `_compile_block` Figure path can't distinguish a
    "whole-node ref to a workflow backend" from "whole-node ref to a
    node" — both look like `name: None`. This pass has the full
    workflow context and can decide: it accepts whole-node refs
    only when the step id resolves to a CompiledBackendStep.
    """
    backend_step_ids = {
        s.id for s in cw.steps if isinstance(s, CompiledBackendStep)
    }

    def _check_block(block: CompiledBlock, node_id: str) -> None:
        if block.type == "figure":
            ref = block.props.get("data_from") or {}
            target = ref.get("node")
            name = ref.get("name")
            if name is None and target not in backend_step_ids:
                # Whole-node ref to something that ISN'T a workflow
                # backend step — author probably meant
                # `steps.<node>.<backend_fn>`.
                raise ValueError(
                    f"node {node_id!r}: Figure data uses a "
                    f"whole-node reference `steps.{target}` to a "
                    f"node — name a backend: "
                    f"`steps.{target}.<fn_name>`. Whole-node refs "
                    f"are only valid when pointing at a workflow-"
                    f"level @backend step (whose return IS the "
                    f"value)."
                )
        for child in block.children:
            _check_block(child, node_id)

    for s in cw.steps:
        if isinstance(s, CompiledNode):
            _check_block(s.layout, s.id)
        elif isinstance(s, CompiledPage):
            for n in s.nodes:
                _check_block(n.layout, n.id)


def _build_permission_template(wf: Workflow, cw: CompiledWorkflow) -> None:
    """Walk the workflow's nodes (and inputs) collecting every Role
    referenced. Validate uniqueness of identifiers, strict-mode
    consistency, and per-input role references.

    Populates `cw.permission_template`:

        {"roles": [<id>, ...],
         "default_role_mode": "open" | "strict"}

    Mutates `cw` in place.
    """
    from .core import (
        Node as _NodeCls, Page as _PageCls, Operator as _OpCls,
    )

    # Strict mode = `@form(default_role=None)`.
    strict_mode = wf.default_role is None
    default_role_mode = "strict" if strict_mode else "open"

    # Track Role objects by identifier; flag when two different
    # objects share an identifier (a workflow author bug).
    role_by_id: dict[str, Any] = {}
    declaration_order: list[str] = []

    def _record_role(role_obj: Any, *, context: str) -> None:
        ident = role_obj.identifier
        prior = role_by_id.get(ident)
        if prior is None:
            role_by_id[ident] = role_obj
            declaration_order.append(ident)
        elif prior is not role_obj:
            raise ValueError(
                f"Workflow {wf.id!r}: role identifier {ident!r} "
                f"was declared by two different Role objects "
                f"(seen at {context}). Each role identifier must "
                "be declared once — share the same Role object "
                "across references."
            )

    def _walk_operator_tree(op: Any, *, ctx: str) -> None:
        """Visit every Operator in a layout tree (including container
        children + downstream-chain operators), recording any
        per-input `role=` references."""
        if op is None:
            return
        if isinstance(op, _OpCls):
            if getattr(op, "role", None) is not None:
                _record_role(op.role, context=f"{ctx} input")
            for child in getattr(op, "downstream", ()) or ():
                _walk_operator_tree(child, ctx=ctx)
        # Layout containers expose children differently — try
        # common attribute names, ignore the rest.
        for attr in ("children", "items"):
            for child in getattr(op, attr, ()) or ():
                _walk_operator_tree(child, ctx=ctx)

    def _iter_source_nodes() -> list[tuple[_NodeCls, str]]:
        out: list[tuple[_NodeCls, str]] = []
        for step in wf.steps:
            if isinstance(step, _NodeCls):
                out.append((step, f"node {step.id!r}"))
            elif isinstance(step, _PageCls):
                for sec in step.nodes:
                    out.append((
                        sec,
                        f"section {sec.id!r} of page {step.id!r}",
                    ))
        return out

    # Register any role declared at the form level so it appears in
    # the permission_template even if no node explicitly references
    # it. (Authors who write `@form(role={...})` and rely entirely
    # on inheritance otherwise produce a workflow with the role
    # invisible to the permission_template; not a runtime error,
    # but a real authoring inconsistency that surfaces in the
    # `/permissions` introspection.)
    if wf.role is not None:
        for r in wf.role.write_roles:
            _record_role(r, context=f"form {wf.id!r} (write)")
        for r in wf.role.read_roles:
            _record_role(r, context=f"form {wf.id!r} (read)")

    for node_obj, ctx in _iter_source_nodes():
        # Strict-mode check: every node must have role= EITHER on
        # the node itself OR inherited from the form. Strict mode
        # with a form-level role is still satisfied — every node
        # has a real role at compile time. Strict mode WITHOUT a
        # form-level role demands node-level declarations.
        if (
            strict_mode
            and node_obj.role is None
            and wf.role is None
        ):
            raise ValueError(
                f"Workflow {wf.id!r}: @form(default_role=None) is "
                f"strict mode, but {ctx} has no `role=` declaration. "
                "Either add role= to the node, or remove "
                "default_role=None from @form."
            )
        if node_obj.role is not None:
            for r in node_obj.role.write_roles:
                _record_role(r, context=f"{ctx} (write)")
            for r in node_obj.role.read_roles:
                _record_role(r, context=f"{ctx} (read)")
        # Walk the layout once to register input-level roles.
        _walk_operator_tree(node_obj.layout, ctx=ctx)

    cw.permission_template = {
        "roles": declaration_order,
        "default_role_mode": default_role_mode,
    }


def validate_assign_references(
    cw: CompiledWorkflow,
    registry: dict[str, CompiledWorkflow],
) -> None:
    """Validate every Assign operator in the workflow against the
    cross-form state visible only after all forms are compiled.

    Called from `scan_workflows` after the registry is complete.
    Per-Assign checks:

      - `form` must be a key in the registry.
      - `role` must be an identifier in the child form's
        permission_template["roles"].
      - `to_ref_descriptor` must reference a PickerInput field on
        the parent node (rejects free-text inputs with a message
        pointing the author to @users.email).

    Raises ValueError with the originating node + form id so the
    author can find the broken Assign.
    """
    for step in cw.steps:
        nodes = _flatten_nodes(step)
        for n in nodes:
            for a in n.assigns:
                _validate_one_assign(cw, n, a, registry)


def _flatten_nodes(step: Any) -> list[CompiledNode]:
    if isinstance(step, CompiledNode):
        return [step]
    if isinstance(step, CompiledPage):
        return list(step.nodes)
    return []


def _validate_one_assign(
    cw: CompiledWorkflow,
    parent_node: CompiledNode,
    a: CompiledAssign,
    registry: dict[str, CompiledWorkflow],
) -> None:
    where = (
        f"workflow {cw.id!r}, node {parent_node.id!r}, "
        f"Assign[op_idx={a.op_idx}]"
    )
    # 1. form_id must exist.
    child = registry.get(a.form_id)
    if child is None:
        raise ValueError(
            f"{where}: Assign(form={a.form_id!r}) — no workflow "
            f"with that form_id is registered."
        )
    # 2. role must be in child's permission_template.
    child_roles = set(child.permission_template.get("roles", []))
    if a.role_id not in child_roles:
        raise ValueError(
            f"{where}: Assign(role={a.role_id!r}) — child form "
            f"{a.form_id!r} declares roles {sorted(child_roles)!r}; "
            f"no role named {a.role_id!r}."
        )
    # 3. `to` ref must point at a PickerInput on the parent node.
    desc = a.to_ref_descriptor
    if desc.get("kind") == "literal":
        # A literal value (bare list of ids) is allowed for
        # programmatic use; skip the picker check.
        return
    ref_node = desc.get("node")
    ref_name = desc.get("name")
    # The descriptor must reference a field on the SAME node — Assign
    # reads a freshly-submitted picker value.
    if ref_node != parent_node.id:
        raise ValueError(
            f"{where}: Assign(to=...) must reference a field on the "
            f"same node ({parent_node.id!r}); got steps.{ref_node}."
            f"{ref_name}."
        )
    field = next(
        (f for f in parent_node.fields if f.name == ref_name), None,
    )
    if field is None:
        raise ValueError(
            f"{where}: Assign(to=steps.{ref_node}.{ref_name}) — no "
            f"input named {ref_name!r} on node {parent_node.id!r}."
        )
    if field.type != "picker":
        raise ValueError(
            f"{where}: Assign(to=steps.{ref_node}.{ref_name}) — "
            f"input is type {field.type!r}, but Assign requires a "
            f"picker. Use @users.email if you want to assign by "
            f"email address."
        )
    # 5. The child form's submission_id_template — if it carries any
    # `steps.<X>.<Y>` references — must be satisfiable. The child's
    # landing-step form_values are seeded from this Assign's prefill
    # AND from whatever the assignee types into the child's landing
    # node. So a `steps.<X>.<Y>` ref is "reachable" when either:
    #   - X is a node on the child form and Y is an input on it
    #     (assignee fills it normally), OR
    #   - X is the child's landing node id AND Y is a key in this
    #     parent's `Assign.prefill={...}` (parent prefills it
    #     directly into landing_step.form_values).
    # A ref pointing at a node that doesn't exist on the child form
    # at all is almost always the author confusing parent and child
    # node names. Without this check, the child stays a draft
    # forever (no minted id) and the cause is invisible until an
    # operator notices the missing id; or — if the parent runs the
    # template successfully on the FIRST submit because of an
    # earlier prefill — it fails at parent-submit time. Catch it at
    # scan time instead.
    template = child.submission_id_template
    if template:
        child_landing = child.landing_node()
        # _CHILD_TPL_REF_RE captures (node, field) pairs from
        # `steps.<node>.<field>` — matches the runtime's
        # `_STEPS_REF_RE` extended to grab the field segment too.
        refs = _CHILD_TPL_REF_RE.findall(template)
        for ref_node_id, ref_field_name in refs:
            child_node = child.by_id.get(ref_node_id)
            if child_node is None:
                raise ValueError(
                    f"{where}: child form {a.form_id!r} has a "
                    f"submission_id_template referencing "
                    f"steps.{ref_node_id}.{ref_field_name}, but "
                    f"the child form has no node named "
                    f"{ref_node_id!r}. (Did you mean a node from "
                    f"the parent? The template renders against "
                    f"the CHILD's steps.)"
                )
            field_on_node = any(
                f.name == ref_field_name for f in child_node.fields
            )
            prefilled_on_landing = (
                ref_node_id == child_landing.id
                and ref_field_name in a.prefill_descriptors
            )
            if not field_on_node and not prefilled_on_landing:
                raise ValueError(
                    f"{where}: child form {a.form_id!r} has a "
                    f"submission_id_template referencing "
                    f"steps.{ref_node_id}.{ref_field_name}, but "
                    f"node {ref_node_id!r} on the child form has "
                    f"no input named {ref_field_name!r}"
                    + (
                        f" and this Assign's prefill={{...}} "
                        f"doesn't supply it either."
                        if ref_node_id == child_landing.id
                        else "."
                    )
                )


# `steps.<node>.<field>` reference capture for the child's
# submission_id_template validation. Mirrors runtime's
# _STEPS_REF_RE but captures both segments.
_CHILD_TPL_REF_RE = re.compile(
    r"steps\s*\.\s*([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)"
)


@dataclass(frozen=True)
class StepDep:
    """One dependency of a node on a value from another node — every
    `steps.<node>.<field>` reference its layout makes.

    `source` records the site, and `kind` (derived) records the
    consequence when the referenced value changes:

      - "functional" (an `options` source or a `When` condition) —
        the reference shapes this node's *inputs*. A change can leave
        the node's submitted data invalid: it must be re-confirmed.
      - "display" (a `default` seed, or a `{{ template }}` in a label
        or url) — the reference only affects what's *shown*. A change
        leaves the data valid but possibly stale: review, not refill.

    This is the static half of edit's dependency analysis. Backend
    `steps` reads are deliberately absent — a `@backend` body is opaque,
    so its dependence is resolved dynamically by re-running it and
    diffing the return, not from this graph.
    """

    node: str  # the upstream node named
    # The input id / backend-return read from it. None for a whole-node
    # reference (`steps.<node>`) — depends on everything in the node.
    field: Optional[str]
    source: str  # "options" | "default" | "condition" | "template" | "argument"
    # The id of the input in *this* node that carries the dependency —
    # set for `options` / `default` (the bite-check needs the field's
    # value); None for conditions, templates, and backend arguments.
    local: Optional[str] = None

    _KIND = {
        "options": "functional",
        "condition": "functional",
        "argument": "functional",
        "default": "display",
        "template": "display",
    }

    @property
    def kind(self) -> str:
        return self._KIND[self.source]

    @property
    def is_whole_node(self) -> bool:
        """True for a `steps.<node>` reference — matches any change in
        the node."""
        return self.field is None


def _collect_deps(block: "CompiledBlock") -> list["StepDep"]:
    """Every `steps.<node>.<field>` dependency in a compiled layout —
    `options_from` / `default_from` descriptors, the cross-node
    conditions of `When` blocks, and `{{ steps.X.Y }}` templates in
    string props. Deduplicated; order is not significant."""
    seen: dict[StepDep, None] = {}

    def add(dep: StepDep) -> None:
        seen.setdefault(dep, None)

    def walk(b: CompiledBlock) -> None:
        # options_from / default_from descriptors — `b` is the input,
        # so `b.id` is the local field the dependency belongs to.
        for key, source in (("options_from", "options"),
                             ("default_from", "default")):
            ref = b.props.get(key)
            if isinstance(ref, dict) and "node" in ref:
                add(StepDep(ref["node"], ref.get("name"), source,
                            local=b.id))
        # Cross-node conditions on a When block.
        if b.type == "when":
            for cond in b.props.get("conditions", []):
                if isinstance(cond, dict) and "node" in cond:
                    add(StepDep(cond["node"], cond["field"], "condition"))
        # {{ steps.X.Y }} (and {{ steps.X.Y.Z }}) templates in
        # label / url props. The dep is on the node — `<Y>` is the
        # member name; the optional `<Z>` is a nested access on a
        # chain step's state dict (not separately tracked).
        for key in TEMPLATED_PROPS:
            value = b.props.get(key)
            if isinstance(value, str) and "{{" in value:
                for node, fld, _sub in STEP_REF_RE.findall(value):
                    add(StepDep(node, fld, "template"))
        for child in b.children:
            walk(child)

    walk(block)
    return list(seen)


def _collect_chain_deps(
    chain: list["CompiledChainStep"],
) -> list["StepDep"]:
    """Every `steps.<node>.<field>` dependency the node's *execution
    chain* introduces — operator config templates and backend-call
    arguments. Complement to `_collect_deps`, which only walks the
    visual layout tree. Without this, an operator like
    `TriggerDag(run_id="{{ steps.intake.ref }}")` looked unaffected
    when `steps.intake` was edited, because the layout walker never
    sees the operator's config.

    Operator config refs are classified as `argument` — they need
    the operator to re-execute, not just be reviewed, since stale
    config drives stale external work (an Airflow run_id that no
    longer matches the form's data). Conservative by design: even a
    cosmetic ref like `waiting_message` re-runs the operator. That's
    one extra rerun in a rare case in exchange for never letting a
    functional ref slip through silently.

    Backend-call args inside a node's chain reference their *chain
    sibling* op ids (not `steps.X.Y`), so they're walked separately
    — they don't escape the chain and aren't picked up here.
    """
    seen: dict[StepDep, None] = {}

    def add(dep: StepDep) -> None:
        seen.setdefault(dep, None)

    def scan_value(v: Any) -> None:
        # Walk into dicts/lists — operator config can have a nested
        # `conf` dict for trigger_dag, and any operator may grow
        # nested config in the future. Bare strings are scanned for
        # {{ steps.X.Y }} templates.
        if isinstance(v, str):
            if "{{" in v:
                for node, fld, _sub in STEP_REF_RE.findall(v):
                    add(StepDep(node, fld, "argument"))
        elif isinstance(v, dict):
            for inner in v.values():
                scan_value(inner)
        elif isinstance(v, list):
            for inner in v:
                scan_value(inner)
        # other types (numbers, bools, None) carry no template refs.

    for cs in chain:
        if cs.kind == "external_task" and cs.external_task is not None:
            scan_value(cs.external_task.config)
        # backend_call args inside a node chain reference chain sibling
        # ids, not `steps.X.Y` — nothing for us here.

    return list(seen)


def _validate_step_refs(cw: CompiledWorkflow) -> None:
    """Check every `steps.<node>.<field>` reference a node makes:

      - the named node must exist;
      - an `options` / `default` / condition reference must name an
        *earlier* node — not the node itself, and not from the entry
        node (which has nothing upstream);
      - a `{{ template }}` reference is exempt from those two rules:
        naming the current node is how a live, in-browser template is
        written, so self-reference there is expected.

    Whether a non-template reference names a genuine ancestor isn't
    enforced statically — if it doesn't, it resolves to nothing at
    runtime."""
    # Valid top-level reference targets in the `steps.<x>` namespace:
    # form nodes and standalone backend steps. Node-internal operators
    # and `@backend`s are reached via `steps.<owning_node>.<step_id>`
    # — that second-level name isn't checked here; if it doesn't
    # exist, the reference resolves to nothing at runtime.
    known = set(cw.all_nodes_by_id) | {s.id for s in cw.steps}
    landing_id = cw.landing_node().id
    for node in cw.all_nodes_by_id.values():
        deps = _collect_deps(node.layout)
        # Functional + default refs read submitted upstream data, so
        # they're invalid on the entry node and may not be self-refs.
        upstream_deps = [d for d in deps if d.source != "template"]
        if upstream_deps and node.id == landing_id:
            raise ValueError(
                f"node {node.id!r} is the workflow's entry node — it has "
                f"no upstream nodes, so `steps` references aren't valid "
                f"in its options / defaults / conditions."
            )
        for dep in deps:
            if dep.node not in known:
                raise ValueError(
                    f"node {node.id!r}: `steps` reference to unknown "
                    f"node {dep.node!r}."
                )
            if dep.source != "template" and dep.node == node.id:
                raise ValueError(
                    f"node {node.id!r}: a `steps` reference in its "
                    f"{dep.source} points at the node itself — it must "
                    f"name an earlier node."
                )


def _validate_backend_step_args(cw: CompiledWorkflow) -> None:
    """Check the `steps` arguments of every workflow-level backend step:
    each must name a step that exists and isn't the step itself, and no
    more positional arguments may be passed than the function declares.
    Whole-node and field references are both allowed here — a backend
    may depend on a single value or an entire node."""
    known = set(cw.all_nodes_by_id) | {s.id for s in cw.steps}
    for s in cw.steps:
        if not isinstance(s, CompiledBackendStep):
            continue
        for ref in list(s.arg_refs) + list(s.kwarg_refs.values()):
            target = ref.get("node")
            if target not in known:
                raise ValueError(
                    f"backend step {s.id!r}: `steps` argument names "
                    f"unknown step {target!r}."
                )
            if target == s.id:
                raise ValueError(
                    f"backend step {s.id!r}: a `steps` argument points "
                    f"at the step itself."
                )
        params = s.fn.param_names
        if len(s.arg_refs) > len(params):
            raise ValueError(
                f"backend step {s.id!r}: {len(s.arg_refs)} arguments "
                f"passed but {s.fn.name!r} takes {len(params)}."
            )
        for name in s.kwarg_refs:
            if name not in params:
                raise ValueError(
                    f"backend step {s.id!r}: {s.fn.name!r} has no "
                    f"parameter {name!r}."
                )


def _is_workflow_terminal(cw: CompiledWorkflow, node: CompiledNode) -> bool:
    """Whether reaching `node` ends the whole workflow — a top-level
    node with no downstream, or a page's terminal section node whose
    page itself has no downstream."""
    page = cw.node_page.get(node.id)
    if page is None:
        return not node.downstream
    return node.id in page.terminal_node_ids and not page.downstream


def _validate_node_buttons(cw: CompiledWorkflow) -> None:
    """Every node needs a Button to advance the flow — except the
    workflow's final screen, which may be a buttonless completion
    node (it completes the moment it's reached)."""
    for node in cw.all_nodes_by_id.values():
        if node.buttons:
            continue
        if not _is_workflow_terminal(cw, node):
            raise ValueError(
                f"node {node.id!r} has no Button — every node that "
                "isn't the workflow's final screen needs one to advance "
                "the flow."
            )
        if node.fields:
            raise ValueError(
                f"terminal node {node.id!r} is buttonless but has input "
                "fields — a buttonless completion screen auto-completes "
                "on arrival and can't submit input. Add a Button, or "
                "drop the fields."
            )


def _compile_page(p: Page, *, form_default_role=None) -> CompiledPage:
    if not p.nodes:
        raise ValueError(f"page {p.id!r} has no section nodes")

    nodes = [
        _compile_node(n, form_default_role=form_default_role)
        for n in p.nodes
    ]
    in_page = {n.id for n in nodes}

    # Page-internal edges must stay within the page.
    for cn in nodes:
        for dst in cn.downstream:
            if dst not in in_page:
                raise ValueError(
                    f"section node {cn.id!r} in page {p.id!r} is wired "
                    f"to {dst!r}, which is not a section node of this page"
                )

    entry_id = p.entry_node().id
    terminal_ids = [cn.id for cn in nodes if not cn.downstream]
    if not terminal_ids:
        raise ValueError(
            f"page {p.id!r} has no end node — every section node is "
            "wired onward, so the page can never complete. One section "
            "node must be terminal (no `>>` to another section)."
        )

    return CompiledPage(
        id=p.id,
        is_flat=p.is_flat,
        title=p.title or _humanize(p.id),
        nodes=nodes,
        entry_node_id=entry_id,
        terminal_node_ids=terminal_ids,
        downstream=[d.id for d in p.downstream],
    )


def _compile_backend_step(bs: BackendStep) -> CompiledBackendStep:
    group = getattr(bs, "group", None) or {}
    return CompiledBackendStep(
        id=bs.id,
        fn=bs.backend_fn,
        is_branch=bs.backend_fn.is_branch,
        hidden=bs.hidden,
        group_id=group.get("id"),
        group_title=group.get("title"),
        arg_refs=[a.serialize() for a in bs.args],
        kwarg_refs={k: v.serialize() for k, v in bs.kwargs.items()},
        downstream=[d.id for d in bs.downstream],
    )


def _step_is_branch(step: Any) -> bool:
    if isinstance(step, CompiledBackendStep):
        return step.is_branch
    if isinstance(step, CompiledNode):
        return _node_is_branch(step)
    if isinstance(step, CompiledPage):
        # A flat page is a single-node screen — its implicit node
        # can host an inner `@backend.branch` just like any @node.
        # Without this branch the workflow-level edge validator
        # treats the page as non-branching and rejects fan-out to
        # multiple downstream steps. Sectioned pages don't branch
        # at the workflow level (their internal section graph is
        # what routes inside the page), so leave those at False.
        if step.is_flat and step.nodes:
            return _node_is_branch(step.nodes[0])
    return False


def _validate_edges(cw: CompiledWorkflow) -> None:
    """Static checks on the `>>` execution graph:

      - workflow-level downstream targets must be real steps,
      - the landing step can't be downstream of anything (it's the entry),
      - only a @backend.branch may fan out to more than one step,
      - page-internal edges must stay within the page, and a page's
        entry section node can't be an internal downstream target.
    """
    landing_id = cw.landing_step().id
    for step in cw.steps:
        for dst in step.downstream:
            if dst not in cw.by_id:
                raise ValueError(
                    f"step {step.id!r} is wired to {dst!r}, which is not "
                    "a registered step in this workflow"
                )
            if dst == landing_id:
                raise ValueError(
                    f"landing step {landing_id!r} cannot be wired as a "
                    f"downstream of {step.id!r} — it is the workflow's "
                    "entry point."
                )
        if len(step.downstream) > 1 and not _step_is_branch(step):
            raise ValueError(
                f"step {step.id!r} fans out to {len(step.downstream)} "
                f"steps ({step.downstream}) but is not a @backend.branch. "
                "Only a branch step may have multiple downstream steps."
            )

        # An HitlBranch's routes must each name a node wired
        # downstream of the step — same rule as @backend.branch.
        if isinstance(step, CompiledNode):
            for ext in step.external_tasks:
                if ext.kind != "airflow_hitl_branch":
                    continue
                for option, target in (
                    (ext.config or {}).get("routes") or {}
                ).items():
                    if target not in step.downstream:
                        raise ValueError(
                            f"node {step.id!r}: HitlBranch "
                            f"{ext.task_id!r} routes option {option!r} to "
                            f"{target!r}, but {target!r} is not wired "
                            f"downstream (downstream: "
                            f"{step.downstream or '[]'}). Wire it, e.g. "
                            f"`{step.id}() >> [{target}(), ...]`."
                        )

        if isinstance(step, CompiledPage):
            for sn in step.nodes:
                if (
                    len(sn.downstream) > 1
                    and not _node_is_branch(sn)
                ):
                    raise ValueError(
                        f"section node {sn.id!r} in page {step.id!r} fans "
                        f"out to {sn.downstream} but is not a "
                        "@backend.branch."
                    )
                if step.entry_node_id in sn.downstream:
                    raise ValueError(
                        f"section node {sn.id!r} is wired to the page's "
                        f"entry node {step.entry_node_id!r}; the entry "
                        "node cannot be a downstream target."
                    )


def _node_is_branch(cn: CompiledNode) -> bool:
    # A node branches if its chain contains a `@backend.branch`, or if
    # it ends in an HitlBranch operator (which routes on the HITL
    # choice). Walks the full chain rather than the singleton view —
    # the branch backend isn't guaranteed to be the first one.
    for cs in cn.chain:
        if cs.kind == "backend_call" and cs.backend_call.fn.is_branch:
            return True
    return any(
        t.kind == "airflow_hitl_branch" for t in cn.external_tasks
    )


def _compile_node(n: Node, *, form_default_role=None) -> CompiledNode:
    if n.layout is None:
        raise ValueError(f"node {n.id!r} has no layout")

    # One walk of the layout tree: build the CompiledBlock tree and
    # collect the input/button operators encountered. Inputs are
    # collected with the conditions of their enclosing When blocks.
    collected: dict[str, list[Any]] = {"inputs": [], "buttons": []}
    layout = _compile_block(n.layout, collected, n.id, [])

    # A node may be buttonless — but only when it's the workflow's
    # final screen (validated in _validate_node_buttons once the whole
    # graph is known). Here we just collect whatever it has.
    button_ops = collected["buttons"]

    fields = [
        _compile_field(op, conditions)
        for op, conditions in collected["inputs"]
    ]
    buttons = [
        CompiledButton(
            id=op.id or "",
            label=getattr(op, "label", ""),
            advances=getattr(op, "advances", None),
        )
        for op in button_ops
    ]

    chain, backend_call, external_tasks = _walk_execution(button_ops, n.id)

    # Layout deps + chain deps. Layout deps come from the visual tree
    # (templates in display props, condition refs in When blocks,
    # options_from / default_from on inputs). Chain deps come from
    # operator config templates (e.g. an Airflow operator's `run_id`
    # template). Self-refs are dropped — the cascade only walks
    # *downstream* of an edit, so a node referencing its own values
    # never participates and the dep is just noise.
    layout_deps = _collect_deps(layout)
    chain_deps = [
        d for d in _collect_chain_deps(chain) if d.node != n.id
    ]
    # Dedup conservatively: keep the first appearance of each
    # (node, field, source) tuple. Layout deps win if a layout
    # template and a chain operator both reference the same upstream
    # field — layout's classification (template = NeedsReview) is
    # the gentler verdict; chain's (argument = NeedsInput) is
    # stricter. Keeping both via dedup logic means both verdicts
    # apply to a single hit, and the strictest wins in
    # `_status_for_step` anyway, so order doesn't matter for the
    # final cascade outcome.
    seen: dict[StepDep, None] = {}
    for d in layout_deps + chain_deps:
        seen.setdefault(d, None)

    # Per-node role declaration — flatten to {"write": [ids], "read": [ids]}.
    # When the node has no role= declaration AND the form has no
    # form-level role= either, leave the field as None (the form's
    # default_role_mode in permission_template decides what happens
    # at runtime). When the form HAS a role= and the node doesn't,
    # the node inherits the form's role wholesale — write_roles
    # and read_roles both flow through. Node-level role= always
    # overrides; no merging.
    effective_role = n.role if n.role is not None else form_default_role
    node_role: Optional[dict[str, list[str]]] = None
    if effective_role is not None:
        node_role = {
            "write": [r.identifier for r in effective_role.write_roles],
            "read": [r.identifier for r in effective_role.read_roles],
        }

    # Collect Assign operators reachable from this node's buttons.
    # Cross-form role validation runs later (it needs the child
    # form's compile output, which may not exist yet at this point);
    # here we serialize the operator into its compiled form so the
    # workflow-load pass can validate against the registry.
    compiled_assigns = _collect_assigns(button_ops, n.id)

    return CompiledNode(
        id=n.id,
        title=n.title or _humanize(n.id),
        closes=getattr(n, "closes", True),
        layout=layout,
        fields=fields,
        buttons=buttons,
        chain=chain,
        backend_call=backend_call,
        external_tasks=external_tasks,
        assigns=compiled_assigns,
        downstream=[d.id for d in n.downstream],
        deps=list(seen),
        role=node_role,
    )


def _collect_assigns(
    button_ops: list[Operator], node_id: str,
) -> list[CompiledAssign]:
    """Walk `>>` downstream from the node's buttons, collecting
    `Assign` operators in declared order. Each gets an `op_idx`
    that's its 0-based position in this node — used as a stable
    key for the child submission's `parent_assign_op_idx` column.

    The picker reference (`to`) is serialized as a step-ref
    descriptor; prefill values are serialized in the same way as
    template props (literals pass through; step refs become
    descriptors).
    """
    from .references import StepRef
    seen_ids: set[int] = set()
    order: list[Assign] = []
    frontier: list[Operator] = []
    for b in button_ops:
        frontier.extend(b.downstream)
    while frontier:
        op = frontier.pop(0)
        if id(op) in seen_ids:
            continue
        seen_ids.add(id(op))
        frontier.extend(op.downstream)
        if isinstance(op, Assign):
            order.append(op)

    out: list[CompiledAssign] = []
    for idx, a in enumerate(order):
        # Serialize the `to` reference. We expect a StepRef
        # (steps.<node>.<input>); record its descriptor so the
        # runtime can resolve against the submission's data.
        to_desc: dict[str, Any]
        if isinstance(a.to_ref, StepRef):
            to_desc = a.to_ref.serialize()
        else:
            # Tolerate plain values for v1 (e.g. a bare list of
            # user_ids for ad-hoc assignment from a fixed source).
            # The runtime will see kind='literal' and skip
            # resolution.
            to_desc = {"kind": "literal", "value": a.to_ref}
        # Prefill: serialize step refs; pass literals through.
        prefill_desc: dict[str, Any] = {}
        for k, v in a.prefill.items():
            if isinstance(v, StepRef):
                prefill_desc[k] = v.serialize()
            else:
                prefill_desc[k] = {"kind": "literal", "value": v}
        out.append(
            CompiledAssign(
                form_id=a.form_id,
                role_id=a.role_id,
                to_ref_descriptor=to_desc,
                prefill_descriptors=prefill_desc,
                link_ttl_days=a.link_ttl_days,
                op_idx=idx,
            )
        )
    return out


def _compile_block(
    op: Any,
    collected: dict,
    node_id: str,
    conditions: list,
) -> "CompiledBlock":
    """Compile one operator to a block, attaching any `.with_comments()`
    thread so the UI can render the comment affordance on the
    component. Recursion (containers) re-enters here, so nested
    components carry their attachments too."""
    blk = _compile_block_inner(op, collected, node_id, conditions)
    thread = getattr(op, "_comment_thread", None)
    if thread and blk is not None:
        blk.props["comment_thread"] = thread
    return blk


def _compile_block_inner(
    op: Operator,
    collected: dict[str, list[Any]],
    node_id: str,
    conditions: list[Any],
) -> CompiledBlock:
    """Recursively compile a layout-tree operator into a CompiledBlock,
    accumulating input/button operators into `collected`.

    `conditions` is the list of FieldConditions from the enclosing
    `When` blocks. It is threaded down so every field can be stamped
    with the conjunction that governs its visibility.
    """

    # --- Conditional container (When) ---
    # Checked before the generic Container case: a When carries its own
    # condition and deepens the condition stack for its children.
    if isinstance(op, When):
        child_conditions = conditions + op.conditions
        return CompiledBlock(
            type="when",
            props={
                "conditions": [c.serialize() for c in op.conditions],
            },
            children=[
                _compile_block(c, collected, node_id, child_conditions)
                for c in op.children
            ],
        )

    # --- Containers ---
    # Collapsible first — it has a two-region child layout the generic
    # branch can't express. With a summary, children compile as exactly
    # two synthetic columns: [0] the always-visible summary, [1] the
    # toggled body; `has_summary` tells the frontend to slice that way.
    # Without one, children compile flat (all toggled), same as any
    # container.
    if isinstance(op, Collapsible):
        props: dict[str, Any] = {"open": op.open}
        if op.title is not None:
            props["title"] = op.title
        body = [
            _compile_block(c, collected, node_id, conditions)
            for c in op.children
        ]
        if op.summary is not None:
            props["has_summary"] = True
            children = [
                _compile_block(op.summary, collected, node_id, conditions),
                CompiledBlock(type="column", props={}, children=body),
            ]
        else:
            children = body
        return CompiledBlock(
            type="collapsible", props=props, children=children,
        )

    if isinstance(op, Container):
        props: dict[str, Any] = {}
        if isinstance(op, (Card, Section)) and op.title is not None:
            props["title"] = op.title
        if isinstance(op, Callout):
            props["variant"] = op.variant
        if isinstance(op, Grid):
            props["columns"] = op.columns
            props["align"] = op.align
        if isinstance(op, Cell):
            props["span"] = op.span
        return CompiledBlock(
            type=op.kind,
            props=props,
            children=[
                _compile_block(c, collected, node_id, conditions)
                for c in op.children
            ],
        )

    # --- Inputs ---
    if isinstance(op, Input):
        collected["inputs"].append((op, conditions))
        props = {
            "label": op.label or _humanize(op.id or ""),
            "required": op.required,
            "placeholder": op.placeholder,
        }
        if op.default is not None:
            props["default"] = op.default
        # Type-specific config — options, date bounds, grid rows/columns.
        props.update(op.extra_props())
        if conditions:
            props["conditions"] = [c.serialize() for c in conditions]
        # An `steps.<node>.<field>` reference passed as options/default
        # — or as a Sankey column — becomes a resolve-at-runtime
        # descriptor; the runtime fills the real value when it serves
        # the node.
        for key in ("options", "default", "column_a", "column_b"):
            ref = props.get(key)
            if isinstance(ref, StepRef):
                if ref.is_whole_node:
                    raise ValueError(
                        f"node {node_id!r}: field {op.id or op.label!r} "
                        f"uses a whole-node reference `steps.{ref.node_id}` "
                        f"as its {key} — that resolves to the node's whole "
                        f"value dict, which has no meaning here. Name a "
                        f"field: `steps.{ref.node_id}.<field>`."
                    )
                del props[key]
                props[f"{key}_from"] = ref.serialize()
        return CompiledBlock(type=op.field_type, id=op.id, props=props)

    # --- Distribution filter (filterable histogram, an interactive input) ---
    if isinstance(op, DistributionFilter):
        collected["inputs"].append((op, conditions))
        props = {
            "label": op.label,
            "required": op.required,
            "value_label": op.value_label,
        }
        # Data: a literal dict is baked into the compiled block; a
        # StepRef becomes a resolve-at-runtime descriptor (same shape
        # as the `options_from` / `column_a_from` pattern for inputs).
        if isinstance(op.data, StepRef):
            if op.data.is_whole_node:
                raise ValueError(
                    f"node {node_id!r}: DistributionFilter "
                    f"{op.id!r} uses a whole-node reference "
                    f"`steps.{op.data.node_id}` as its data — name a "
                    f"field: `steps.{op.data.node_id}.<field>`."
                )
            props["data_from"] = op.data.serialize()
        else:
            props["data"] = op.data
        if conditions:
            props["conditions"] = [c.serialize() for c in conditions]
        return CompiledBlock(
            type="histogram_widget",
            id=op.id or "",
            props=props,
        )

    # --- Categorizer (drag items from a bank into category columns) ---
    if isinstance(op, Categorizer):
        collected["inputs"].append((op, conditions))
        props = {
            "label": op.label,
            "required": op.required,
            "categories": op.categories,
            "bank_label": op.bank_label,
        }
        # The item bank rides the same `options` / `options_from`
        # convention as choice inputs, so the runtime's existing
        # upstream-reference resolution covers it with no new code.
        if isinstance(op.options, StepRef):
            if op.options.is_whole_node:
                raise ValueError(
                    f"node {node_id!r}: Categorizer {op.id!r} uses a "
                    f"whole-node reference `steps.{op.options.node_id}` "
                    f"as its options — name a field: "
                    f"`steps.{op.options.node_id}.<field>`."
                )
            props["options_from"] = op.options.serialize()
        else:
            props["options"] = op.options
        if conditions:
            props["conditions"] = [c.serialize() for c in conditions]
        return CompiledBlock(
            type="widget_categorizer",
            id=op.id or "",
            props=props,
        )

    # --- Redistribution editor (bucket-to-bucket mapping widget) ---
    if isinstance(op, RedistributionEditor):
        collected["inputs"].append((op, conditions))
        props = {
            "label": op.label,
            "required": op.required,
            "policies": op.policies,
            "default_policy": op.default_policy,
            "value_label": op.value_label,
        }
        # Each of `data`, `sources`, `destinations` may be a literal
        # or a StepRef. Same pattern as DistributionFilter — the
        # literal is baked into the compiled block; the StepRef
        # becomes a `*_from` descriptor resolved at runtime.
        for field_name in ("data", "sources", "destinations"):
            val = getattr(op, field_name)
            if isinstance(val, StepRef):
                if val.is_whole_node:
                    raise ValueError(
                        f"node {node_id!r}: RedistributionEditor "
                        f"{op.id!r} uses a whole-node reference "
                        f"`steps.{val.node_id}` as its {field_name} "
                        f"— name a field: "
                        f"`steps.{val.node_id}.<field>`."
                    )
                props[f"{field_name}_from"] = val.serialize()
            else:
                props[field_name] = val
        if conditions:
            props["conditions"] = [c.serialize() for c in conditions]
        return CompiledBlock(
            type="redistribution_widget",
            id=op.id or "",
            props=props,
        )

    # --- Button ---
    if isinstance(op, Button):
        props = {"label": op.label, "variant": op.variant}
        if op.url is not None:
            # A link button navigates instead of submitting — it's pure
            # layout, not collected as one of the node's submit buttons,
            # and so it can't carry a `>>` execution chain.
            if op.downstream:
                raise ValueError(
                    f"node {node_id!r}: link button "
                    f"{op.id or op.label!r} has a `>>` chain — a link "
                    f"button navigates to a URL and doesn't submit, so "
                    f"nothing can run after it."
                )
            props["url"] = op.url
            props["new_tab"] = op.new_tab
        else:
            collected["buttons"].append(op)
        return CompiledBlock(type="button", id=op.id, props=props)

    # --- Display leaves ---
    if isinstance(op, Markdown):
        return CompiledBlock(type="markdown", props={"source": op.source})
    if isinstance(op, Divider):
        return CompiledBlock(type="divider")
    if isinstance(op, Image):
        return CompiledBlock(
            type="image",
            props={"src": op.src, "alt": op.alt, "caption": op.caption},
        )
    if isinstance(op, KPI):
        # `label` and `value` are both templated — same TEMPLATED_PROPS
        # machinery as Markdown's source / Image's caption.
        return CompiledBlock(
            type="kpi",
            props={"label": op.label, "value": op.value},
        )
    if isinstance(op, Comments):
        # A comment thread anchored to this point in the layout. The
        # block only carries its thread id + labels; the frontend
        # loads and posts via the comments API.
        return CompiledBlock(
            type="comments",
            id=op.id or "",
            props={"label": op.label, "placeholder": op.placeholder},
        )
    if isinstance(op, KPIGroups):
        # Data-driven collapsible KPI sections. A literal dict is
        # baked into the compiled block; a StepRef becomes a
        # `data_from` descriptor the runtime resolves — the same
        # pattern as DistributionFilter's data. Whole-node refs are
        # allowed (a workflow-scope backend step's namespace IS its
        # return), mirroring Figure.
        props = {"open": op.open}
        if isinstance(op.data, StepRef):
            props["data_from"] = op.data.serialize()
        else:
            props["data"] = op.data
        return CompiledBlock(
            type="kpi_groups",
            id=op.id or "",
            props=props,
        )
    if isinstance(op, Figure):
        # The block carries a `data_from` descriptor pointing at the
        # bytes-returning source. At render time the resolver looks
        # up the value (a blob handle dict) and the frontend builds
        # a proxy URL from `handle.hash`.
        #
        # Two valid shapes:
        #   - field ref `steps.<node>.<backend_fn>` — common: a node-
        #     internal @backend returning bytes
        #   - whole-node ref `steps.<workflow_backend_step>` — when
        #     the bytes come from a workflow-level @backend step
        #     (its `steps.<step_id>` namespace IS its return value)
        # The inline compile path can't distinguish these without
        # the full workflow context. `_validate_figure_data_refs`
        # runs later (in `compile_workflow`) and rejects whole-node
        # refs that don't point at a workflow-level backend step —
        # catching the common author error of `steps.<node>` where
        # `steps.<node>.<backend>` was meant.
        props: dict[str, Any] = {
            "data_from": op.data.serialize(),
            "alt": op.alt,
            "caption": op.caption,
        }
        if op.width is not None:
            props["width"] = op.width
        if op.height is not None:
            props["height"] = op.height
        return CompiledBlock(type="figure", props=props)
    if isinstance(op, S3Download):
        # Bucket, connection, expires_in are static; the key and
        # filename are templated and resolved at render time. The
        # block carries no URL — the click goes through the form
        # server's download proxy, which generates a fresh presigned
        # URL on demand (with response-content-disposition embedded
        # when `filename` is set).
        return CompiledBlock(
            type="s3_download",
            id=op.id,
            props={
                "bucket": op.bucket,
                "key": op.key,
                "label": op.label,
                "connection": op.connection,
                "expires_in": op.expires_in,
                "filename": op.filename,
            },
        )
    if isinstance(op, Dashboard):
        # Only the NAME travels. The embed UUID and refresh-filter id are
        # resolved at render time via /api/dashboards/{name}/embed —
        # compiled trees are snapshotted per form_version, so a baked-in
        # UUID would pin a form to whatever dashboard existed when it was
        # written.
        return CompiledBlock(
            type="dashboard",
            id=op.id,
            props={
                "name": op.name,
                "connection": op.connection,
                "height": op.height,
                "show_filters": op.show_filters,
                "filters_expanded": getattr(op, "filters_expanded", False),
                "declared_filters": [
                    f.serialize() for f in getattr(op, "filters", [])
                ],
            },
        )
    if isinstance(op, Table):
        data = op.func()
        if not isinstance(data, dict):
            raise TypeError(
                f"@displays.table function {op.func.__name__!r} must "
                f"return a dict, got {type(data).__name__}"
            )
        return CompiledBlock(
            type="table", props={"title": op.title, "data": data}
        )

    raise TypeError(
        f"{type(op).__name__} cannot appear in a layout tree (node "
        f"{node_id!r}). @backend calls and external tasks belong in the "
        "`>>` graph, not the returned layout."
    )


def _walk_execution(
    button_ops: list[Operator], node_id: str
) -> tuple[
    list[CompiledChainStep],
    Optional[CompiledBackendCall],
    list[CompiledExternalTask],
]:
    """Walk `>>` downstream from the buttons to discover the execution
    graph: an ordered chain of backend calls and external tasks.

    Multiple `@backend` calls per node are allowed; they interleave
    with operators by declared `>>` order. At most one of them may be
    a `@backend.branch` — branch-routing has a single source of
    truth (lifting this is roadmapped). Standalone `@backend.branch`
    nodes are unaffected.

    Returns the canonical `chain` plus two derived views for older
    read-side code: the *first* backend call (the legacy singleton)
    and the flat list of external tasks.
    """
    seen: set[int] = set()
    order: list[Operator] = []
    frontier: list[Operator] = []
    for b in button_ops:
        frontier.extend(b.downstream)
    while frontier:
        op = frontier.pop(0)
        if id(op) in seen:
            continue
        seen.add(id(op))
        order.append(op)
        frontier.extend(op.downstream)

    # Lift the old "at most one @backend per node" limit — backends
    # are now first-class chain steps and can interleave with
    # operators. But keep "at most one branch backend per chain" —
    # the routing decision must have one source of truth.
    branch_backends = [
        o for o in order
        if isinstance(o, BackendCall) and o.backend_fn.is_branch
    ]
    if len(branch_backends) > 1:
        raise ValueError(
            f"node {node_id!r} has {len(branch_backends)} "
            f"@backend.branch calls in its chain; at most one is "
            f"allowed (a chain has one routing source of truth)."
        )

    chain: list[CompiledChainStep] = []
    backend_calls: list[CompiledBackendCall] = []
    external_tasks: list[CompiledExternalTask] = []
    # Track ids of chain steps seen *so far*. A backend `defers_to_chain`
    # when (a) one of its arg ids matches an earlier chain step
    # (operator task_id or earlier backend fn_name), OR (b) any earlier
    # chain step itself defers — either an operator (always) or an
    # already-deferred backend. Rule (b) preserves declared `>>` order:
    # a backend placed after a deferred predecessor must wait for it,
    # even if the backend's own args are all form fields.
    seen_chain_step_ids: set[str] = set()
    chain_has_deferred_step = False
    for op in order:
        if isinstance(op, BackendCall):
            arg_op_ids = [a.id or "" for a in op.args]
            arg_refs_chain_step = any(
                aid in seen_chain_step_ids for aid in arg_op_ids
            )
            defers = arg_refs_chain_step or chain_has_deferred_step
            bc = CompiledBackendCall(
                fn=op.backend_fn,
                arg_op_ids=arg_op_ids,
                defers_to_chain=defers,
            )
            backend_calls.append(bc)
            chain.append(CompiledChainStep(
                kind="backend_call", backend_call=bc,
            ))
            seen_chain_step_ids.add(op.backend_fn.name)
            if defers:
                chain_has_deferred_step = True
        elif isinstance(op, ExternalTask):
            et = _compile_external_task(op)
            external_tasks.append(et)
            chain.append(CompiledChainStep(
                kind="external_task", external_task=et,
            ))
            seen_chain_step_ids.add(et.task_id)
            # An operator always defers the rest of the chain — even
            # if a downstream backend doesn't reference it, the `>>`
            # order says it should run after.
            chain_has_deferred_step = True

    # Legacy singleton view: the first backend (None if no backends).
    legacy_backend = backend_calls[0] if backend_calls else None
    return chain, legacy_backend, external_tasks



def _compile_filter_values(filters: dict, task_id: str) -> dict:
    """Filter values, with any `steps.<node>.<field>` reference turned
    into a resolve-at-runtime descriptor.

    A template (`"{{ steps.a.b }}"`) always renders to a STRING, which
    is right for one value and wrong for a list — a multi-select's
    answer would arrive as `"['East', 'West']"`. A StepRef keeps the
    value's own type, so a list stays a list.
    """
    from .references import StepRef

    compiled: dict = {}
    for name, value in filters.items():
        if isinstance(value, StepRef):
            if value.is_whole_node:
                raise ValueError(
                    f"superset.SetFilters {task_id!r}: filter {name!r} "
                    f"uses a whole-node reference "
                    f"`steps.{value.node_id}` — name a field: "
                    f"`steps.{value.node_id}.<field>`."
                )
            compiled[name] = {"__step_ref__": value.serialize()}
        elif isinstance(value, (list, tuple)):
            compiled[name] = [
                {"__step_ref__": v.serialize()}
                if isinstance(v, StepRef) else v
                for v in value
            ]
        else:
            compiled[name] = value
    return compiled

def _compile_external_task(op: ExternalTask) -> CompiledExternalTask:
    # Superset first: it is the one external operator with no Airflow
    # behind it, and the isinstance chain below is Airflow-specific.
    # Imported lazily so the optional integration cannot break compile
    # for installs that never use it.
    try:
        from ..superset.operators import (
            RefreshDashboard as _RefreshDashboard,
            SetFilters as _SetFilters,
        )
    except Exception:  # noqa: BLE001 - optional integration
        _RefreshDashboard = ()  # type: ignore[assignment]
        _SetFilters = ()  # type: ignore[assignment]

    if _SetFilters and isinstance(op, _SetFilters):
        return CompiledExternalTask(
            task_id=op.id or "",
            kind=op.kind,
            graph_visible=op.graph_visible,
            hidden=getattr(op, "hidden", False),
            retryable=op.retryable,
            config={
                "connection": op.connection,
                "dashboard": op.name,
                "panel": op.panel,
                # Values stay unrendered here: a form_version is
                # snapshotted, and these reference steps that have not
                # run yet. They are resolved when the chain reaches this
                # operator.
                "filters": _compile_filter_values(op.filters, op.id or ""),
            },
        )

    if _RefreshDashboard and isinstance(op, _RefreshDashboard):
        return CompiledExternalTask(
            task_id=op.id or "",
            kind=op.kind,
            graph_visible=op.graph_visible,
            hidden=getattr(op, "hidden", False),
            retryable=op.retryable,
            config={
                "connection": op.connection,
                "dashboard": op.name,
            },
        )
    if isinstance(op, AirflowStatus):
        return CompiledExternalTask(
            task_id=op.task_id,
            kind="airflow_status",
            config={
                "dag_id": op.dag_id,
                "run_id_template": op.run_id_template,
            },
        )
    if isinstance(op, TriggerDag):
        return CompiledExternalTask(
            task_id=op.id or "",
            kind=op.kind,
            graph_visible=op.graph_visible,
            hidden=getattr(op, "hidden", False),
            retryable=op.retryable,
            config={
                "connection": op.connection,
                "dag_id": op.dag_id,
                "conf": op.conf,
                # Optional templated run id; None → Airflow auto-generates.
                "run_id_template": op.run_id_template,
                "waiting_message": op.waiting_message,
            },
        )
    if isinstance(op, TaskStateSensor):
        return CompiledExternalTask(
            task_id=op.id or "",
            kind=op.kind,
            graph_visible=op.graph_visible,
            hidden=getattr(op, "hidden", False),
            retryable=op.retryable,
            poll_interval_ms=op.poll_interval_ms,
            config={
                "connection": op.connection,
                "dag_id": op.dag_id,
                "task_id": op.task_id,
                "run_id_template": op.run_id_template,
                # Raw Airflow states the sensor matches against.
                "target_states": op.target_states,
                "waiting_message": op.waiting_message,
            },
        )
    if isinstance(op, TaskSensor):
        return CompiledExternalTask(
            task_id=op.id or "",
            kind=op.kind,
            graph_visible=op.graph_visible,
            hidden=getattr(op, "hidden", False),
            retryable=op.retryable,
            poll_interval_ms=op.poll_interval_ms,
            config={
                "connection": op.connection,
                "dag_id": op.dag_id,
                "task_id": op.task_id,
                "run_id_template": op.run_id_template,
                "waiting_message": op.waiting_message,
            },
        )
    if isinstance(op, DagSensor):
        return CompiledExternalTask(
            task_id=op.id or "",
            kind=op.kind,
            graph_visible=op.graph_visible,
            hidden=getattr(op, "hidden", False),
            retryable=op.retryable,
            poll_interval_ms=op.poll_interval_ms,
            config={
                "connection": op.connection,
                "dag_id": op.dag_id,
                "run_id_template": op.run_id_template,
                "waiting_message": op.waiting_message,
            },
        )
    if isinstance(op, XComPull):
        return CompiledExternalTask(
            task_id=op.id or "",
            kind=op.kind,
            graph_visible=op.graph_visible,
            hidden=getattr(op, "hidden", False),
            retryable=op.retryable,
            poll_interval_ms=op.poll_interval_ms,
            config={
                "connection": op.connection,
                "dag_id": op.dag_id,
                "task_id": op.task_id,
                "run_id_template": op.run_id_template,
                "key": op.key,
                "waiting_message": op.waiting_message,
            },
        )
    if isinstance(op, HitlBranch):
        return CompiledExternalTask(
            task_id=op.id or "",
            kind=op.kind,
            graph_visible=op.graph_visible,
            hidden=getattr(op, "hidden", False),
            retryable=op.retryable,
            poll_interval_ms=op.poll_interval_ms,
            config={
                "connection": op.connection,
                "dag_id": op.dag_id,
                "task_id": op.task_id,
                "run_id_template": op.run_id_template,
                # option -> downstream node id, for chain routing.
                "routes": op.routes,
                "waiting_message": op.waiting_message,
            },
        )
    if isinstance(op, HitlResponse):
        # chosen_options / params_input may be either literals (sent
        # as-is at runtime) or StepRefs (resolved against `steps` at
        # dispatch). The runtime resolver picks the right path from
        # the *_from descriptors.
        config: dict[str, Any] = {
            "connection": op.connection,
            "dag_id": op.dag_id,
            "task_id": op.task_id,
            "run_id_template": op.run_id_template,
            "waiting_message": op.waiting_message,
        }
        if isinstance(op.chosen_options, StepRef):
            config["chosen_options_from"] = op.chosen_options.serialize()
        else:
            config["chosen_options"] = list(op.chosen_options)
        if op.params_input is None:
            config["params_input"] = None  # send form values as-is
        elif isinstance(op.params_input, StepRef):
            config["params_input_from"] = op.params_input.serialize()
        else:
            config["params_input"] = dict(op.params_input)
        return CompiledExternalTask(
            task_id=op.id or "",
            kind=op.kind,
            graph_visible=op.graph_visible,
            hidden=getattr(op, "hidden", False),
            retryable=op.retryable,
            config=config,
        )
    if isinstance(op, Hitl):
        return CompiledExternalTask(
            task_id=op.id or "",
            kind=op.kind,
            graph_visible=op.graph_visible,
            hidden=getattr(op, "hidden", False),
            retryable=op.retryable,
            poll_interval_ms=op.poll_interval_ms,
            config={
                "connection": op.connection,
                "dag_id": op.dag_id,
                "task_id": op.task_id,
                "run_id_template": op.run_id_template,
                "waiting_message": op.waiting_message,
            },
        )
    raise TypeError(
        f"Unknown ExternalTask subclass {type(op).__name__!r}. "
        "Add a branch in _compile_external_task."
    )


def _compile_field(
    op: Operator, conditions: Optional[list[Any]] = None
) -> CompiledField:
    serialized = [c.serialize() for c in (conditions or [])]
    if isinstance(op, Input):
        # An upstream-backed ChoiceInput has no static options — they're
        # resolved at runtime, so the compiled field carries none.
        options = (
            op.options
            if isinstance(op, ChoiceInput) and isinstance(op.options, list)
            else []
        )
        # File-upload fields carry their server-side upload config.
        file_spec = None
        if op.field_type in ("file", "s3file"):
            file_spec = {
                "kind": op.field_type,
                "max_size_mb": getattr(op, "max_size_mb", 25),
                "accept": getattr(op, "accept", []),
            }
            if op.field_type == "s3file":
                file_spec["bucket"] = getattr(op, "bucket", None)
                file_spec["key"] = getattr(op, "key", "")
        # Per-input write-role gate. Role's identifier (string) is
        # serialized; the actual Role object is referenced via the
        # form's permission_template at runtime.
        input_role = (
            op.role.identifier if op.role is not None else None
        )
        # Picker fields carry an `identifier_kind` describing what
        # the picker produces. Non-picker fields leave it None.
        identifier_kind = getattr(op, "identifier_kind", None)
        return CompiledField(
            name=op.id or "",
            label=op.label or _humanize(op.id or ""),
            type=op.field_type,
            required=op.required,
            options=options,
            conditions=serialized,
            file_spec=file_spec,
            role=input_role,
            identifier_kind=identifier_kind,
        )
    if isinstance(op, DistributionFilter):
        return CompiledField(
            name=op.id or "",
            label=op.label,
            type="widget",
            required=op.required,
            conditions=serialized,
        )
    if isinstance(op, RedistributionEditor):
        return CompiledField(
            name=op.id or "",
            label=op.label,
            type="widget",
            required=op.required,
            conditions=serialized,
        )
    if isinstance(op, Categorizer):
        return CompiledField(
            name=op.id or "",
            label=op.label,
            type="widget",
            required=op.required,
            conditions=serialized,
        )
    raise TypeError(f"{type(op).__name__} is not a field")


def _humanize(s: str) -> str:
    return s.replace("_", " ").strip().capitalize()


# --- Source-unavailable deserialization ------------------------------------
#
# A submission can outlive its form-source's compilability. Library APIs
# drift, files move, imports break, the language evolves — any of which
# can leave a perfectly good submission with a perfectly intact
# audit trail pinned to a `form_version` whose `source` no longer
# re-execs cleanly. The submission's data isn't lost — it's in the
# database. The compiled structure isn't lost either — it's in the
# `form_version.compiled_graph` JSON column, dropped to disk every
# time a new version is recorded.
#
# `compiled_graph_to_workflow` reconstructs a *view-only* CompiledWorkflow
# from that JSON. It has the full layout tree, fields, buttons, and node
# graph — everything needed to RENDER the submission. What it doesn't
# have is executable Python: BackendFn instances become inert
# placeholders that raise WorkflowSourceUnavailable when invoked.
#
# The runtime uses this as a fallback when `compile_source` raises on
# a stored source. The submission stays viewable; advance/submit
# operations are gated separately at the endpoint layer.


class WorkflowSourceUnavailable(RuntimeError):
    """Raised when code tries to execute a backend or branch belonging
    to a workflow whose source could not be re-execed. Carries the
    name of the missing function and the original compile error so
    callers can surface a useful diagnostic."""

    def __init__(self, fn_name: str, source_error: str) -> None:
        super().__init__(
            f"backend {fn_name!r} not available — form source "
            f"failed to compile: {source_error}"
        )
        self.fn_name = fn_name
        self.source_error = source_error


class _UnavailableBackendFn:
    """Stand-in for `BackendFn` when the form source can't be
    re-execed. Has the surface area read by serializers and the
    runtime's introspection paths — `.name`, `.is_branch`, `.hidden`,
    `.retryable`, `.param_names` — but `__call__` raises rather than
    pretending to execute. Duck-typed against BackendFn rather than
    inheriting; we never need the parent's wrap/build machinery."""

    def __init__(
        self,
        *,
        name: str,
        is_branch: bool = False,
        hidden: bool = False,
        retryable: bool = True,
        source_error: str = "",
    ) -> None:
        self.func = None
        self.name = name
        self.is_branch = is_branch
        self.hidden = hidden
        self.retryable = retryable
        # No source means we don't know real param names — empty list
        # is fine: nothing should be calling this anyway.
        self.param_names: list[str] = []
        self.source_error = source_error

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise WorkflowSourceUnavailable(
            fn_name=self.name, source_error=self.source_error,
        )


def _deserialize_block(d: dict[str, Any]) -> CompiledBlock:
    return CompiledBlock(
        type=d["type"],
        id=d.get("id"),
        props=dict(d.get("props") or {}),
        children=[_deserialize_block(c) for c in d.get("children") or []],
    )


def _deserialize_field(d: dict[str, Any]) -> CompiledField:
    # The serialized form drops `conditions`, `file_spec`, and
    # `identifier_kind` — none of those are needed for view rendering
    # (visibility conditions are also stamped in the layout's `When`
    # blocks which DO survive serialization). Reconstruct with defaults.
    return CompiledField(
        name=d["name"],
        label=d["label"],
        type=d["type"],
        required=bool(d.get("required", False)),
        options=list(d.get("options") or []),
        role=d.get("role"),
    )


def _deserialize_node(
    d: dict[str, Any], source_error: str,
) -> CompiledNode:
    backend_call: Optional[CompiledBackendCall] = None
    if d.get("has_backend_call"):
        backend_call = CompiledBackendCall(
            fn=_UnavailableBackendFn(  # type: ignore[arg-type]
                name=d.get("backend_call_name") or "<unknown>",
                source_error=source_error,
            ),
            arg_op_ids=[],
        )
    external_tasks = [
        CompiledExternalTask(
            task_id=t["task_id"],
            kind=t["kind"],
            config=dict(t.get("config") or {}),
            graph_visible=bool(t.get("graph_visible", False)),
            # Absent in graphs written before this was serialized, and
            # False is the right reading of "the author never said".
            hidden=bool(t.get("hidden", False)),
            retryable=bool(t.get("retryable", True)),
        )
        for t in (d.get("external_tasks") or [])
    ]
    return CompiledNode(
        id=d["id"],
        title=d.get("title") or d["id"],
        closes=d.get("closes", True),
        layout=_deserialize_block(d["layout"]),
        fields=[_deserialize_field(f) for f in d.get("fields") or []],
        buttons=[
            CompiledButton(
                id=b["id"], label=b["label"], advances=b.get("advances")
            )
            for b in d.get("buttons") or []
        ],
        chain=[],  # callables dropped; chain isn't reconstructable
        backend_call=backend_call,
        external_tasks=external_tasks,
        assigns=[],  # not serialized; not needed for viewing
        downstream=list(d.get("downstream") or []),
        deps=[],  # cascade deps not serialized; viewing doesn't need them
        role=d.get("role"),
    )


def _deserialize_step(
    d: dict[str, Any], source_error: str,
) -> Any:
    kind = d.get("step_kind")
    if kind == "page":
        return CompiledPage(
            id=d["id"],
            is_flat=bool(d.get("is_flat", False)),
            title=d.get("title") or d["id"],
            nodes=[
                _deserialize_node(n, source_error)
                for n in d.get("nodes") or []
            ],
            entry_node_id=d["entry_node_id"],
            terminal_node_ids=list(d.get("terminal_node_ids") or []),
            downstream=list(d.get("downstream") or []),
        )
    if kind == "backend":
        return CompiledBackendStep(
            id=d["id"],
            fn=_UnavailableBackendFn(  # type: ignore[arg-type]
                name=d.get("fn_name") or d["id"],
                is_branch=bool(d.get("is_branch", False)),
                hidden=bool(d.get("hidden", False)),
                retryable=bool(d.get("retryable", True)),
                source_error=source_error,
            ),
            is_branch=bool(d.get("is_branch", False)),
            hidden=bool(d.get("hidden", False)),
            downstream=list(d.get("downstream") or []),
        )
    # Default: top-level CompiledNode (the serializer emits these via
    # _serialize_node without an explicit `step_kind` discriminator).
    return _deserialize_node(d, source_error)


def compiled_graph_to_workflow(
    graph: dict[str, Any],
    *,
    source_error: str = "",
) -> CompiledWorkflow:
    """Reconstruct a *view-only* CompiledWorkflow from the stored
    `form_version.compiled_graph` JSON.

    The result carries the full structure — layouts, fields, buttons,
    node graph, downstream edges — and is sufficient to render any
    submission detail page, history view, or source tab. Backend
    function callables are replaced with inert `_UnavailableBackendFn`
    placeholders that raise `WorkflowSourceUnavailable` if invoked.

    `source_error` is the message from the original compile failure;
    embedded into the placeholders so it surfaces in the diagnostic
    raised when something tries to advance the chain. Caller passes
    the captured exception text from `compile_source`.

    Use this only as a *fallback* for `compile_source`. When the
    source DOES compile, the live path produces a workflow with real
    callables; this path produces one without. Compare via `getattr(
    wf, 'source_unavailable', False)` if the runtime needs to gate
    behavior on which it has.
    """
    steps = [
        _deserialize_step(s, source_error)
        for s in graph.get("steps") or []
    ]
    cw = CompiledWorkflow(
        id=graph["id"],
        title=graph.get("title") or graph["id"],
        description=graph.get("description") or "",
        submission_id_template=graph.get("submission_id_template"),
        steps=steps,
        tags=list(graph.get("tags") or []),
        iframe_allowed_origins=(
            list(graph["iframe_allowed_origins"])
            if graph.get("iframe_allowed_origins") is not None
            else None
        ),
        permission_template=dict(
            graph.get("permission_template")
            or {"roles": [], "default_role_mode": "open"}
        ),
    )
    # Tag the workflow so advance/submit endpoints can short-circuit
    # with a clear 409 rather than the obscure error a placeholder
    # backend would raise mid-chain.
    cw.source_unavailable = True  # type: ignore[attr-defined]
    cw.source_error = source_error  # type: ignore[attr-defined]
    return cw
