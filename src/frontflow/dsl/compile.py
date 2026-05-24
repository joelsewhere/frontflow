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

from dataclasses import dataclass, field
from typing import Any, Optional

from .actions import Button
from .backend import BackendFn
from .core import (
    BackendCall,
    BackendStep,
    Container,
    Node,
    Operator,
    Page,
    Workflow,
)
from .displays import Callout, Card, Divider, Figure, Image, KPI, Markdown, S3Download, Section, Table
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
from .widgets import DistributionFilter


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


@dataclass
class CompiledButton:
    """One submit-style button extracted from the layout tree."""
    id: str
    label: str


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
    # Whether a user may rerun this operator on its own from the UI.
    retryable: bool = True
    # How often the frontend should poll this operator while in-flight,
    # in milliseconds. None → fall back to the framework default
    # (see useSubmission). Surfaced separately from `config` because
    # this is purely frontend UX; the runtime never reads it.
    poll_interval_ms: int | None = None


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
    chain: list[CompiledChainStep] = field(default_factory=list)
    backend_call: Optional[CompiledBackendCall] = None
    external_tasks: list[CompiledExternalTask] = field(default_factory=list)
    # Execution edges (downstream step ids), from `>>`. For a top-level
    # node these are workflow-level; for a page section node they are
    # page-internal (to sibling section nodes).
    downstream: list[str] = field(default_factory=list)
    # Static `steps.<node>.<field>` dependencies — every upstream value
    # this node's layout reads, tagged functional/display. Drives the
    # dependency-aware cascade when an upstream node is edited.
    deps: list["StepDep"] = field(default_factory=list)


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
        "layout": _serialize_block(n.layout),
        "fields": [
            {
                "name": f.name,
                "label": f.label,
                "type": f.type,
                "required": f.required,
                "options": f.options,
            }
            for f in n.fields
        ],
        "buttons": [{"id": b.id, "label": b.label} for b in n.buttons],
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
    for s in wf.steps:
        if isinstance(s, Page):
            compiled.append(_compile_page(s))
        elif isinstance(s, Node):
            compiled.append(_compile_node(s))
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
    )
    _validate_edges(cw)
    _validate_node_buttons(cw)
    _validate_step_refs(cw)
    _validate_backend_step_args(cw)
    return cw


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


def _compile_page(p: Page) -> CompiledPage:
    if not p.nodes:
        raise ValueError(f"page {p.id!r} has no section nodes")

    nodes = [_compile_node(n) for n in p.nodes]
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
    return CompiledBackendStep(
        id=bs.id,
        fn=bs.backend_fn,
        is_branch=bs.backend_fn.is_branch,
        hidden=bs.hidden,
        arg_refs=[a.serialize() for a in bs.args],
        kwarg_refs={k: v.serialize() for k, v in bs.kwargs.items()},
        downstream=[d.id for d in bs.downstream],
    )


def _step_is_branch(step: Any) -> bool:
    if isinstance(step, CompiledBackendStep):
        return step.is_branch
    if isinstance(step, CompiledNode):
        return _node_is_branch(step)
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


def _compile_node(n: Node) -> CompiledNode:
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
        CompiledButton(id=op.id or "", label=getattr(op, "label", ""))
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

    return CompiledNode(
        id=n.id,
        title=n.title or _humanize(n.id),
        layout=layout,
        fields=fields,
        buttons=buttons,
        chain=chain,
        backend_call=backend_call,
        external_tasks=external_tasks,
        downstream=[d.id for d in n.downstream],
        deps=list(seen),
    )


def _compile_block(
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
    if isinstance(op, Container):
        props: dict[str, Any] = {}
        if isinstance(op, (Card, Section)) and op.title is not None:
            props["title"] = op.title
        if isinstance(op, Callout):
            props["variant"] = op.variant
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
    if isinstance(op, Figure):
        # The block carries a `data_from` descriptor pointing at the
        # bytes-returning @backend. At render time the resolver looks
        # up the backend's return — a blob handle dict — and the
        # frontend builds a proxy URL from `handle.hash`.
        if op.data.is_whole_node:
            raise ValueError(
                f"node {node_id!r}: Figure data uses a whole-node "
                f"reference `steps.{op.data.node_id}` — name a "
                f"backend: `steps.{op.data.node_id}.<fn_name>`."
            )
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
        # Bucket, connection, expires_in are static; the key is
        # templated and resolved at render time. The block carries no
        # URL — the click goes through the form server's download
        # proxy, which generates a fresh presigned URL on demand.
        return CompiledBlock(
            type="s3_download",
            id=op.id,
            props={
                "bucket": op.bucket,
                "key": op.key,
                "label": op.label,
                "connection": op.connection,
                "expires_in": op.expires_in,
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


def _compile_external_task(op: ExternalTask) -> CompiledExternalTask:
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
            retryable=op.retryable,
            config=config,
        )
    if isinstance(op, Hitl):
        return CompiledExternalTask(
            task_id=op.id or "",
            kind=op.kind,
            graph_visible=op.graph_visible,
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
        return CompiledField(
            name=op.id or "",
            label=op.label or _humanize(op.id or ""),
            type=op.field_type,
            required=op.required,
            options=options,
            conditions=serialized,
            file_spec=file_spec,
        )
    if isinstance(op, DistributionFilter):
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
