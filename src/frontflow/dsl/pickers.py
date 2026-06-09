"""Picker decorators — user/group identifier inputs.

Phase 3 of the role-based assignment system. Pickers are
dropdown-style inputs whose options are computed server-side at
render time by a developer-supplied resolver. They produce
identifiers (frontflow user_ids, external IDs, email addresses,
group_ids) — used to drive `Assign(to=...)` in Phase 4.

Surface:

    from frontflow import users

    # Frontflow user_ids — body returns list[int].
    @users(label="Recruiter")
    def recruiter(ctx):
        return [42, 43, 44]

    # External IDs — body returns list[str]. Resolved via the
    # `resolve_external_user` hook from Phase 2.
    @users.external(label="Reviewer", multi=True)
    def reviewers(ctx):
        return [u.sis_id for u in lms.list_recruiters()]

    # Email addresses — body returns list[str]. Match-or-create.
    @users.email(label="Manager")
    def manager(ctx):
        return ["alice@co.com", "bob@co.com"]

    # Frontflow group_ids — body returns list[int].
    @users.groups(label="Team")
    def team(ctx): ...   # empty body → list all groups

Empty body (`pass` / `...`) → built-in resolver lists all
frontflow Users / Groups. The external + email variants have no
built-in (they depend on customer-side systems); empty body
there is a load-time error.

Per-use overrides via call syntax:

    @node
    def emergency():
        return recruiter(required=True, label="Recruiter (required)")

The decorator name encodes `identifier_kind`. `Assign(to=...)`
(Phase 4) reads this at compile time to validate the picker's
identifier type and decide how to resolve picked values into
User rows at execution time.
"""
from __future__ import annotations

import inspect
from typing import Any, Callable, Optional

from .core import Operator, Role
from .inputs import Input


# Sentinel for an unset identifier_kind on the base class — pickers
# always set this explicitly.
_KIND_UNSET = object()


class PickerInput(Input):
    """Base for user / group picker inputs.

    Produced by the `@users`, `@users.external`, `@users.email`,
    `@users.groups` decorators. Carries the resolver function +
    the `identifier_kind` declaring what kind of identifier the
    `value` field produces.

    A picker is constructed by its decorator; node bodies receive
    the picker either by bare reference (the decorator's default
    config) or by call (per-use overrides).
    """

    # `field_type` distinguishes pickers from regular choice inputs
    # at the wire level — the frontend renders a server-resolved
    # dropdown. v1 ships one type; the variants share rendering.
    field_type = "picker"

    # Which identifier kind the resolver returns:
    #   "frontflow_user_id" → list[int]
    #   "external_id"       → list[str]
    #   "email"             → list[str]
    #   "frontflow_group_id"→ list[int]
    # Each subclass sets this; bare instantiation isn't supported.
    identifier_kind: Any = _KIND_UNSET

    def __init__(
        self,
        *,
        resolver: Callable[..., list[Any]],
        id: Optional[str] = None,
        label: Optional[str] = None,
        required: bool = False,
        default: Any = None,
        help: str = "",
        role: Optional[Role] = None,
        multi: bool = False,
    ) -> None:
        super().__init__(
            id=id,
            label=label,
            required=required,
            default=default,
            help=help,
            role=role,
        )
        if self.identifier_kind is _KIND_UNSET:
            # Should never reach this — every concrete subclass
            # sets identifier_kind. Belt-and-braces.
            raise TypeError(
                "PickerInput cannot be instantiated directly; use one "
                "of @users / @users.external / @users.email / "
                "@users.groups"
            )
        self.resolver = resolver
        self.multi = bool(multi)

    def extra_props(self) -> dict[str, Any]:
        props = super().extra_props()
        # The frontend uses identifier_kind + multi to render the
        # right widget; the resolver is server-side and is never
        # serialized. Options resolve via a separate /options
        # endpoint when the user opens the dropdown.
        props["identifier_kind"] = self.identifier_kind
        props["multi"] = self.multi
        return props

    def _derive(self, **overrides: Any) -> "PickerInput":
        """Return a new PickerInput of the same concrete class, with
        per-use overrides applied. Used to enable
        `recruiter(required=True)` in a node body."""
        kwargs = {
            "resolver": self.resolver,
            "id": self.id,
            "label": self.label,
            "required": self.required,
            "default": self.default,
            "help": self.help,
            "role": self.role,
            "multi": self.multi,
        }
        kwargs.update(overrides)
        return type(self)(**kwargs)

    def __call__(self, **overrides: Any) -> "PickerInput":
        """Per-use override — `recruiter(required=True)` returns a
        derived picker with that flag set."""
        return self._derive(**overrides)


class _FrontflowUserPicker(PickerInput):
    identifier_kind = "frontflow_user_id"


class _ExternalUserPicker(PickerInput):
    identifier_kind = "external_id"


class _EmailUserPicker(PickerInput):
    identifier_kind = "email"


class _GroupPicker(PickerInput):
    identifier_kind = "frontflow_group_id"


# --- Built-in resolvers ----------------------------------------------------


def _builtin_users_resolver(ctx: Any) -> list[int]:
    """Default for `@users` with no body. Returns every active
    frontflow user's id. Caller is responsible for any filtering
    via a custom resolver if the install is large."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session as DBSession
    from .store import User, _engine

    with DBSession(_engine) as session:
        rows = session.execute(
            select(User.id).where(User.is_active == True)  # noqa: E712
        ).scalars().all()
        return list(rows)


def _builtin_groups_resolver(ctx: Any) -> list[int]:
    """Default for `@users.groups` with no body. Returns every
    frontflow group's id."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session as DBSession
    from .store import Group, _engine

    with DBSession(_engine) as session:
        rows = session.execute(
            select(Group.id)
        ).scalars().all()
        return list(rows)


# --- Decorator factory -----------------------------------------------------


def _make_picker_decorator(
    picker_cls: type[PickerInput],
    builtin_resolver: Optional[Callable[[Any], list[Any]]] = None,
    *,
    decorator_name: str,
) -> Callable[..., Any]:
    """Build a `@users` / `@users.external` / etc. decorator. The
    decorator can be applied bare (uses the function name as the
    id, no other config) or with kwargs.

    `builtin_resolver` is the default when the decorated function's
    body is trivial (`pass` or `...`). When None, an empty body is a
    load-time error — used for `@users.external` and `@users.email`
    where the customer must supply the resolver.
    """

    def _build(
        fn: Callable[..., list[Any]],
        *,
        label: Optional[str],
        id: Optional[str],
        required: bool,
        default: Any,
        help: str,
        role: Optional[Role],
        multi: bool,
    ) -> PickerInput:
        resolver = fn
        # Detect trivial body — `pass` or `...` only.
        if _is_trivial_body(fn):
            if builtin_resolver is None:
                raise ValueError(
                    f"@{decorator_name}: function {fn.__name__!r} has "
                    "an empty body, but there is no built-in resolver "
                    "for this picker type. Provide a function body "
                    "that returns the list of identifiers."
                )
            resolver = builtin_resolver
        return picker_cls(
            resolver=resolver,
            id=id or fn.__name__,
            label=label,
            required=required,
            default=default,
            help=help,
            role=role,
            multi=multi,
        )

    def decorator(
        func: Optional[Callable[..., list[Any]]] = None,
        /,
        *,
        label: Optional[str] = None,
        id: Optional[str] = None,
        required: bool = False,
        default: Any = None,
        help: str = "",
        role: Optional[Role] = None,
        multi: bool = False,
    ) -> Any:
        # `@users` bare (no parens, no kwargs) — `func` is the
        # decorated function. Build the picker directly.
        if func is not None:
            return _build(
                func,
                label=label, id=id, required=required,
                default=default, help=help, role=role, multi=multi,
            )

        # `@users(...)` — return a deferred decorator with kwargs
        # bound.
        def _deferred(fn: Callable[..., list[Any]]) -> PickerInput:
            return _build(
                fn,
                label=label, id=id, required=required,
                default=default, help=help, role=role, multi=multi,
            )

        return _deferred

    decorator.__name__ = decorator_name
    return decorator


def _is_trivial_body(fn: Callable[..., Any]) -> bool:
    """True if the function's body is just `pass` / `...` / a
    docstring (and nothing else). Used to detect "use the built-in
    resolver" intent.

    Two checks in order:

      1. Source inspection via `inspect.getsource` + AST parse —
         works for functions defined in importable modules.
      2. Bytecode inspection as a fallback — works for functions
         defined inside an `exec`'d code blob (form files loaded
         by `scan_workflows`'s `_exec_form_source`), where
         `inspect.getsource` raises OSError.

    The bytecode check is conservative: a trivial body compiles to
    a single constant-return (Python 3.11+ uses RETURN_CONST; earlier
    versions LOAD_CONST + RETURN_VALUE). If the bytecode pattern
    doesn't match the trivial shape, we fall back to "not trivial"
    and run the user's function.
    """
    import ast
    import textwrap

    # Source-level check first — preferred when available.
    try:
        source = inspect.getsource(fn)
        source = textwrap.dedent(source)
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError):
        # Source not inspectable — fall through to bytecode.
        return _is_trivial_body_bytecode(fn)

    if not tree.body:
        return False
    func_def = tree.body[0]
    if not isinstance(
        func_def, (ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        return False
    body = func_def.body
    # Strip a leading docstring.
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if not body:
        return True  # only a docstring
    if len(body) == 1:
        stmt = body[0]
        if isinstance(stmt, ast.Pass):
            return True
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and stmt.value.value is Ellipsis
        ):
            return True
    return False


def _is_trivial_body_bytecode(fn: Callable[..., Any]) -> bool:
    """Fallback for `_is_trivial_body` when source isn't
    inspectable. Inspects the function's compiled bytecode for a
    trivial-body pattern.

    A trivial body (`pass`, `...`, docstring-only) compiles to one
    of these instruction sequences depending on Python version:

      - Py 3.12+:    RESUME, RETURN_CONST None  (2 instructions)
      - Py 3.11:     RESUME, LOAD_CONST None, RETURN_VALUE  (3)
      - Py <= 3.10:  LOAD_CONST None, RETURN_VALUE  (2)

    Anything else (a real expression, assignment, call, ...) adds
    at least one more instruction. The check counts non-trivial
    operations rather than matching exact bytecode strings — keeps
    it robust across Python versions.
    """
    import dis
    try:
        instructions = list(dis.get_instructions(fn))
    except Exception:  # noqa: BLE001
        return False
    # Allow only RESUME, LOAD_CONST None, RETURN_VALUE, RETURN_CONST.
    # Anything else means the body did work.
    allowed_opnames = {
        "RESUME", "LOAD_CONST", "RETURN_VALUE", "RETURN_CONST",
    }
    for ins in instructions:
        if ins.opname not in allowed_opnames:
            return False
        # If a LOAD_CONST is for anything other than None, the
        # body produced a value (e.g., return 42, or even just
        # `42` as an expression statement). Trivial bodies only
        # produce None.
        if ins.opname == "LOAD_CONST" and ins.argval is not None:
            return False
        if ins.opname == "RETURN_CONST" and ins.argval is not None:
            return False
    return True


# --- Public decorator surface ----------------------------------------------


class _UsersNamespace:
    """`users` is itself a decorator (frontflow user_ids) AND has
    attributes (`.external`, `.email`, `.groups`) which are their
    own decorators. Implements `__call__` for the bare form and
    exposes the others as static class attrs.
    """

    def __init__(self) -> None:
        self._main = _make_picker_decorator(
            _FrontflowUserPicker,
            builtin_resolver=_builtin_users_resolver,
            decorator_name="users",
        )
        self.external = _make_picker_decorator(
            _ExternalUserPicker,
            builtin_resolver=None,
            decorator_name="users.external",
        )
        self.email = _make_picker_decorator(
            _EmailUserPicker,
            builtin_resolver=None,
            decorator_name="users.email",
        )
        self.groups = _make_picker_decorator(
            _GroupPicker,
            builtin_resolver=_builtin_groups_resolver,
            decorator_name="users.groups",
        )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._main(*args, **kwargs)


users = _UsersNamespace()
