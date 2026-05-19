"""
@backend and @backend.branch decorators.

A @backend function runs logic. Its return value is stored under
`steps.<id>` so later steps (and Jinja templates) can read it.

It has two roles, decided by *where it's called*:

  - Inside a node body — `submit >> trigger(name)` — it's a node's
    submit action: wired to a button via `>>`, receiving operator
    arguments. This is a BackendCall.

  - At workflow scope — `collect() >> notify(steps.collect.email)` —
    it's a standalone workflow step: runs automatically when the flow
    reaches it, with no screen. This is a BackendStep. The values it
    consumes are passed explicitly as `steps` references at the call
    site, so its dependencies are visible to the compiler.

Inside a node body a @backend receives operator arguments. At workflow
scope it receives `steps` references — `steps.<node>.<field>` for one
value, or a whole-node `steps.<node>` for the node's entire value dict
(the deliberate "depends on everything in this node").

@backend.branch additionally routes: its return value can be a node id
(jump there), END (terminate), or None (fall through).

`@backend(hidden=True)` hides a workflow-step's marker in the chain UI.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Optional

from .core import (
    BackendCall,
    BackendStep,
    BackendStepRef,
    _building_node,
    _current_workflow,
)


class BackendFn:
    """A wrapped @backend function.

    Calling it inside a node body yields a BackendCall (node-internal).
    Calling it at workflow scope registers and yields a BackendStep.
    The user never instantiates this directly; @backend produces one.
    """

    def __init__(
        self,
        func: Callable[..., Any],
        *,
        is_branch: bool,
        hidden: bool = False,
        retryable: bool = True,
    ) -> None:
        self.func = func
        self.name = func.__name__
        self.is_branch = is_branch
        # Whether a workflow-step marker is shown in the chain UI.
        self.hidden = hidden
        # Whether a user may rerun *this step on its own* from the UI.
        # When False, the step's failed-state menu offers no per-step
        # rerun — but an upstream reset still cascades through it.
        self.retryable = retryable
        # Parameter names — the runtime uses these to bind operator
        # args positionally and to detect the magic `steps` parameter.
        sig = inspect.signature(func)
        self.param_names = list(sig.parameters.keys())

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if _building_node.get():
            # Inside a node body — a submit action wired to a button.
            return BackendCall(self, args, kwargs)

        # Workflow scope — a standalone backend step. No operators are
        # in scope here; the values it consumes are passed explicitly as
        # `steps` references, bound positionally to its parameters.
        from .references import StepRef

        for a in (*args, *kwargs.values()):
            if not isinstance(a, StepRef):
                raise TypeError(
                    f"@backend {self.name!r} is a workflow step "
                    f"(`{self.name}(...)` in the >> chain); its arguments "
                    f"must be `steps` references — `steps.<node>.<field>` "
                    f"or a whole-node `steps.<node>` — not {a!r}."
                )
        step = BackendStep(self, args, kwargs, hidden=self.hidden)
        wf = _current_workflow.get()
        if wf is not None:
            wf.add_backend_step(step)
        return BackendStepRef(step)

    def __repr__(self) -> str:
        kind = "backend.branch" if self.is_branch else "backend"
        return f"<{kind} {self.name}>"


class _BackendDecorator:
    """Exposes `@backend` and `@backend.branch`, each usable bare or
    with arguments:

        @backend
        def f(): ...

        @backend(hidden=True)
        def f(): ...

        @backend.branch
        def route(): ...
    """

    def __call__(
        self,
        func: Optional[Callable[..., Any]] = None,
        /,
        *,
        hidden: bool = False,
        retryable: bool = True,
    ) -> Any:
        def deco(fn: Callable[..., Any]) -> BackendFn:
            return BackendFn(
                fn, is_branch=False, hidden=hidden, retryable=retryable
            )

        return deco(func) if func is not None else deco

    def branch(
        self,
        func: Optional[Callable[..., Any]] = None,
        /,
        *,
        hidden: bool = False,
        retryable: bool = True,
    ) -> Any:
        def deco(fn: Callable[..., Any]) -> BackendFn:
            return BackendFn(
                fn, is_branch=True, hidden=hidden, retryable=retryable
            )

        return deco(func) if func is not None else deco


backend = _BackendDecorator()
