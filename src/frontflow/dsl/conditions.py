"""
Conditional layout — the `When` container, field conditions, and the
`@displays.branch` decorator.

Two ways to make part of a node's layout conditional:

  1. `displays.When(condition, *children)` — the primitive. Its children
     render only when the condition holds, evaluated live against the
     form's current values.

         displays.When(pet.equals("Yes"), pet_kind, vax_date)

  2. `@displays.branch` — ergonomic sugar that compiles down to `When`.
     The decorated function receives its controlling field(s) and is
     written as ordinary `if` / `elif` / `else`:

         @displays.branch
         def pet_followups(pet):
             if pet == "Cat":
                 return (litter_type,)
             elif pet == "Dog":
                 return (breed, walk_schedule)
             else:
                 return (other_notes,)

     The field arrives as a *recording proxy*: the `if` you test isn't
     truly evaluated, it's captured. The decorator runs the body once
     per branch — scripting each fork — to walk every path, and emits a
     `When` per branch carrying the conditions that lead to it.

A condition is built off a controlling field operator —
`pet.equals("Yes")`, `pet.in_(["Cat", "Dog"])` (see the builder methods
on the Input base) — or, inside a `@displays.branch` body, off the
proxy via `==`, `!=`, or a bare truthiness test.

A branch (or an explicit `When`) may also condition on an *earlier*
node's value. A `@displays.branch` body declares a magic `steps`
parameter and drills into upstream data — `steps.<node>.<field>`:

    @displays.branch
    def followup(steps):
        if steps.intake.contact_method == "Email":
            return (email_field,)

Such cross-node conditions are resolved server-side against the
submission's data when the node is served — `steps` references work
the same way for explicit `When` (`steps.intake.x.equals(...)`).
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Callable, Optional

from .core import Container, Operator, _capture_locals, _normalize_layout
from .references import StepRef


# --- Conditions ------------------------------------------------------------


class FieldCondition:
    """A single test on a field's value.

    Same-node: `field` is the controlling Operator (its id is resolved
    at compile time, once variable-name capture has run) and `node` is
    None — the condition is evaluated live, client-side, against the
    current form.

    Cross-node: `field` is a field-id string and `node` is the id of an
    earlier node — the condition is resolved server-side against that
    upstream node's submitted value (it can't change while the current
    node is being filled).

    `op` is one of: equals, not_equals, in, not_in, contains, truthy,
    falsy, button_clicked. The last one is special — the "field" is a
    Button (not an Input), and the condition holds when that button was
    the one the user clicked to submit the node. Used to gate
    after-submit content (status callouts, confirmation messages).
    """

    _NEGATE = {
        "equals": "not_equals",
        "not_equals": "equals",
        "in": "not_in",
        "not_in": "in",
        "contains": "contains",
        "truthy": "falsy",
        "falsy": "truthy",
        # button_clicked has no natural inverse — there's no
        # "any-other-button" semantic worth supporting. Negation just
        # round-trips to itself; the form author writes a sibling When
        # for the other path.
        "button_clicked": "button_clicked",
    }

    def __init__(
        self,
        field: "Operator | str",
        op: str,
        value: Any = None,
        node: Optional[str] = None,
    ) -> None:
        if op not in self._NEGATE:
            raise ValueError(f"unknown condition op {op!r}")
        self.field = field
        self.op = op
        self.value = value
        self.node = node

    def negate(self) -> "FieldCondition":
        return FieldCondition(
            self.field, self._NEGATE[self.op], self.value, node=self.node
        )

    def serialize(self) -> dict[str, Any]:
        """Compile-time form — the field id, op, and operand, plus the
        upstream node id for a cross-node condition. Shipped to the
        frontend (same-node) or resolved server-side (cross-node)."""
        field_id = (
            self.field if isinstance(self.field, str) else self.field.id
        )
        d: dict[str, Any] = {
            "field": field_id,
            "op": self.op,
            "value": self.value,
        }
        if self.node is not None:
            d["node"] = self.node
        return d

    def __repr__(self) -> str:
        if isinstance(self.field, str):
            ref = f"{self.node}.{self.field}"
        else:
            ref = repr(getattr(self.field, "id", None))
        return f"<FieldCondition {ref} {self.op} {self.value!r}>"


# --- The When container ----------------------------------------------------


def _normalize_when_children(children: tuple[Any, ...]) -> list[Operator]:
    """Children passed to `When` — operators, or tuples/lists that
    expand into containers (the usual layout shorthand)."""
    out: list[Operator] = []
    for c in children:
        if c is None:
            continue
        if isinstance(c, (tuple, list)):
            out.append(_normalize_layout(c))
        elif isinstance(c, Operator):
            out.append(c)
        else:
            raise TypeError(
                f"When children must be display elements or tuples of "
                f"them; got {type(c).__name__}."
            )
    return out


class When(Container):
    """A container whose children render only when its condition holds.

    The condition is one `FieldCondition` or a conjunction (a list of
    them — all must hold). The compiler stamps every descendant field
    with the accumulated conditions, so visibility, validation, and
    payload pruning all agree.
    """

    kind = "when"

    def __init__(
        self,
        condition: "FieldCondition | list[FieldCondition]",
        *children: Any,
    ) -> None:
        super().__init__(*_normalize_when_children(children))
        if isinstance(condition, FieldCondition):
            self.conditions: list[FieldCondition] = [condition]
        else:
            self.conditions = list(condition)

    def __repr__(self) -> str:
        return (
            f"<When conditions={len(self.conditions)} "
            f"children={len(self.children)}>"
        )


# --- Recording proxies (for @displays.branch) ------------------------------

# The recorder active while a @displays.branch body is being traced.
_branch_recorder: ContextVar[Optional["_BranchRecorder"]] = ContextVar(
    "branch_recorder", default=None
)


class _BranchRecorder:
    """Drives one run of a branch body along a scripted path, recording
    the condition tested at each fork."""

    def __init__(self, script: list[bool]) -> None:
        self.script = script
        self.taken: list[bool] = []
        self.conditions: list[FieldCondition] = []
        self.new_forks: list[list[bool]] = []

    def decide(self, condition: FieldCondition) -> bool:
        idx = len(self.taken)
        self.conditions.append(condition)
        if idx < len(self.script):
            choice = self.script[idx]
        else:
            # A fork not covered by the script — take True now, and
            # queue the sibling path that takes False here.
            self.new_forks.append(list(self.taken) + [False])
            choice = True
        self.taken.append(choice)
        return choice


def _decide(condition: FieldCondition) -> bool:
    rec = _branch_recorder.get()
    if rec is None:
        # Used outside @displays.branch tracing — shouldn't happen, but
        # fall back to a definite answer rather than crashing.
        return False
    return rec.decide(condition)


class _ConditionProxy:
    """A pending condition inside a branch body. `if <this>:` triggers
    `__bool__`, which records the condition and returns the scripted
    decision."""

    def __init__(self, condition: FieldCondition) -> None:
        self._condition = condition

    def __bool__(self) -> bool:
        return _decide(self._condition)


class _FieldProxy:
    """Stand-in for a controlling field inside a `@displays.branch`
    body. Comparisons build conditions; a bare truthiness test (`if
    pet:`) is itself a condition. The target is a same-node Operator or
    an upstream reference reached through the magic `steps` parameter.
    """

    def __init__(self, target: "Operator | StepRef") -> None:
        self._target = target

    def _cond(self, op: str, value: Any) -> FieldCondition:
        """Build a condition — same-node off an Operator, cross-node off
        an upstream reference (`steps.<node>.<field>`)."""
        t = self._target
        if isinstance(t, StepRef):
            return FieldCondition(t.name, op, value, node=t.node_id)
        return FieldCondition(t, op, value)

    # Comparisons → pending conditions.
    def __eq__(self, other: Any) -> Any:  # type: ignore[override]
        return _ConditionProxy(self._cond("equals", other))

    def __ne__(self, other: Any) -> Any:  # type: ignore[override]
        return _ConditionProxy(self._cond("not_equals", other))

    __hash__ = None  # type: ignore[assignment]

    # Explicit builders — mirror the Input methods, usable in the body.
    def equals(self, value: Any) -> _ConditionProxy:
        return _ConditionProxy(self._cond("equals", value))

    def not_equals(self, value: Any) -> _ConditionProxy:
        return _ConditionProxy(self._cond("not_equals", value))

    def in_(self, values: Any) -> _ConditionProxy:
        return _ConditionProxy(self._cond("in", list(values)))

    def not_in(self, values: Any) -> _ConditionProxy:
        return _ConditionProxy(self._cond("not_in", list(values)))

    def is_filled(self) -> _ConditionProxy:
        return _ConditionProxy(self._cond("truthy", None))

    def is_blank(self) -> _ConditionProxy:
        return _ConditionProxy(self._cond("falsy", None))

    # Bare `if pet:` — the field being truthy is itself the condition.
    def __bool__(self) -> bool:
        return _decide(self._cond("truthy", None))


class _StepsNodeProxy:
    """`steps.<node>` inside a branch body — a further attribute selects
    a field, yielding a recording proxy for a cross-node condition.

    The proxy also *is* a whole-node reference: `steps.<node>` used on
    its own (not narrowed to a field) declares that the branch depends
    on the entire node. `whole_node_ref` exposes that for the compiler's
    dependency collection."""

    def __init__(self, node_id: str) -> None:
        self._node_id = node_id
        # The branch reads `steps.<node>` without narrowing — recorded
        # so the compiler can register a whole-node dependency.
        self.whole_node_ref = StepRef(node_id)

    def __getattr__(self, field: str) -> _FieldProxy:
        if field.startswith("_"):
            raise AttributeError(field)
        return _FieldProxy(StepRef(self._node_id, field))

    def __getitem__(self, field: str) -> _FieldProxy:
        return _FieldProxy(StepRef(self._node_id, field))


class _StepsProxy:
    """The magic `steps` parameter of a `@displays.branch` body —
    `steps.<node>.<field>` reaches an upstream node's value, so a branch
    can be conditioned on what an earlier node captured."""

    def __getattr__(self, node_id: str) -> _StepsNodeProxy:
        if node_id.startswith("_"):
            raise AttributeError(node_id)
        return _StepsNodeProxy(node_id)

    def __getitem__(self, node_id: str) -> _StepsNodeProxy:
        return _StepsNodeProxy(node_id)


# --- The branch decorator --------------------------------------------------

# Safety bound on path exploration — guards against pathological nesting.
_MAX_BRANCH_RUNS = 256


def _normalize_branch_result(result: Any) -> list[Operator]:
    """Turn what a branch body returned into a list of layout
    operators. `None` (a branch with no `return`, or `return None`)
    yields nothing."""
    if result is None:
        return []
    if isinstance(result, (tuple, list)):
        return [
            _normalize_layout(x) for x in result if x is not None
        ]
    if isinstance(result, Operator):
        return [result]
    raise TypeError(
        f"a @displays.branch body must return display elements (or a "
        f"tuple of them); got {type(result).__name__}."
    )


class _BranchTemplate:
    """Result of `@displays.branch`. Calling it with the controlling
    field(s) traces the body's `if` / `elif` / `else` and produces the
    `When` blocks."""

    def __init__(self, func: Callable[..., Any]) -> None:
        self.func = func
        self.id = func.__name__
        # Parameter order, so call-time controlling fields bind
        # positionally and the magic `steps` parameter is detected.
        import inspect

        self._params = list(inspect.signature(func).parameters)

    def _bind(self, args: tuple[Any, ...]) -> list[Any]:
        """Map the call's controlling fields onto the body's parameters.
        A parameter named `steps` is magic — it receives the cross-node
        accessor and consumes none of `args`; every other parameter
        takes the next controlling field, wrapped for recording."""
        proxies: list[Any] = []
        supplied = iter(args)
        for name in self._params:
            if name == "steps":
                proxies.append(_StepsProxy())
                continue
            try:
                controlling = next(supplied)
            except StopIteration:
                raise TypeError(
                    f"@displays.branch {self.id!r}: no controlling field "
                    f"given for parameter {name!r}."
                ) from None
            proxies.append(_FieldProxy(controlling))
        extra = list(supplied)
        if extra:
            raise TypeError(
                f"@displays.branch {self.id!r}: got {len(extra)} more "
                f"controlling field(s) than the body has parameters."
            )
        return proxies

    def __call__(self, *args: Any) -> Operator:
        from .displays import Column  # deferred — avoids an import cycle

        proxies = self._bind(args)

        runs: list[tuple[list[tuple[FieldCondition, bool]], Any]] = []
        queue: list[list[bool]] = [[]]
        seen: set[tuple[bool, ...]] = set()

        while queue and len(runs) < _MAX_BRANCH_RUNS:
            script = queue.pop()
            key = tuple(script)
            if key in seen:
                continue
            seen.add(key)

            rec = _BranchRecorder(script)
            token = _branch_recorder.set(rec)
            try:
                result, captured = _capture_locals(self.func, *proxies)
            finally:
                _branch_recorder.reset(token)

            # Assign ids to operators built in this branch, from the
            # variable names they were bound to (same as a node body).
            for var_name, value in captured.items():
                if isinstance(value, Operator) and value.id is None:
                    value.id = var_name

            runs.append((list(zip(rec.conditions, rec.taken)), result))
            queue.extend(rec.new_forks)

        whens: list[When] = []
        for run_conds, result in runs:
            children = _normalize_branch_result(result)
            if not children:
                continue
            conditions = [
                cond if chose else cond.negate()
                for cond, chose in run_conds
            ]
            whens.append(When(conditions, *children))

        if len(whens) == 1:
            return whens[0]
        return Column(*whens)

    def __repr__(self) -> str:
        return f"<BranchTemplate {self.id!r}>"


def branch(func: Callable[..., Any]) -> _BranchTemplate:
    """Decorator: turn a function of `if` / `elif` / `else` over its
    controlling field(s) into conditional layout. See the module
    docstring."""
    return _BranchTemplate(func)
