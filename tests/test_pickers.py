"""Phase 3 tests — picker decorators.

Covers:
  - `@users`, `@users.external`, `@users.email`, `@users.groups`
    produce PickerInput subclasses with the right identifier_kind
  - Bare `@users` (no kwargs) works
  - `@users(label=..., multi=True, ...)` accepts config
  - Empty body uses built-in resolver for `@users` and `@users.groups`
  - Empty body raises for `@users.external` and `@users.email`
  - Resolver carries through; `ctx` is passed
  - Per-use overrides return derived pickers
  - Built-in resolvers query the right tables
  - Pickers usable in node bodies; compile-time threading preserved
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from frontflow import Button, displays, form, inputs, node, users
from frontflow.dsl.core import WORKFLOWS, Role
from frontflow.dsl.compile import compile_workflow
from frontflow.dsl.pickers import (
    PickerInput,
    _ExternalUserPicker,
    _EmailUserPicker,
    _FrontflowUserPicker,
    _GroupPicker,
    _builtin_users_resolver,
    _builtin_groups_resolver,
)
from frontflow.dsl.store import User, Group, _engine


# ----------------------------------------------------------------------------
# Decorator identity-kind
# ----------------------------------------------------------------------------

class TestPickerIdentityKind:
    def test_users_produces_frontflow_user_id_picker(self):
        @users(label="x")
        def p(ctx):
            return [1]
        assert isinstance(p, _FrontflowUserPicker)
        assert p.identifier_kind == "frontflow_user_id"

    def test_users_external_produces_external_picker(self):
        @users.external(label="x")
        def p(ctx):
            return ["sis-1"]
        assert isinstance(p, _ExternalUserPicker)
        assert p.identifier_kind == "external_id"

    def test_users_email_produces_email_picker(self):
        @users.email(label="x")
        def p(ctx):
            return ["a@b.com"]
        assert isinstance(p, _EmailUserPicker)
        assert p.identifier_kind == "email"

    def test_users_groups_produces_group_picker(self):
        @users.groups(label="x")
        def p(ctx):
            return [1]
        assert isinstance(p, _GroupPicker)
        assert p.identifier_kind == "frontflow_group_id"


# ----------------------------------------------------------------------------
# Decorator shapes (bare / config / per-use overrides)
# ----------------------------------------------------------------------------

class TestPickerDecoratorShapes:
    def test_bare_users_decorator_works(self):
        # @users with no parens — `fn` is the decorated function.
        @users
        def p(ctx):
            return [1]
        # Default label is None; id defaults to function name.
        assert p.id == "p"
        assert p.identifier_kind == "frontflow_user_id"

    def test_config_kwargs_apply(self):
        @users(label="Pick someone", required=True, multi=True, help="hint")
        def p(ctx):
            return [1, 2]
        assert p.label == "Pick someone"
        assert p.required is True
        assert p.multi is True
        assert p.help == "hint"

    def test_per_use_override_returns_derived(self):
        @users(label="orig")
        def p(ctx):
            return [1]
        derived = p(required=True, label="override")
        # Derived is a new picker with overrides applied.
        assert derived is not p
        assert derived.required is True
        assert derived.label == "override"
        # Same identifier_kind and resolver.
        assert derived.identifier_kind == p.identifier_kind
        assert derived.resolver is p.resolver

    def test_role_kwarg_threads_through(self):
        approver = Role("approver")
        @users(label="x", role=approver)
        def p(ctx):
            return [1]
        assert p.role is approver

    def test_id_kwarg_overrides_function_name(self):
        @users(label="x", id="custom_id")
        def p(ctx):
            return [1]
        assert p.id == "custom_id"


# ----------------------------------------------------------------------------
# Empty-body / built-in resolver
# ----------------------------------------------------------------------------

class TestEmptyBodyResolvers:
    def test_users_empty_body_uses_builtin(self):
        @users(label="x")
        def p(ctx): ...
        assert p.resolver is _builtin_users_resolver

    def test_users_groups_empty_body_uses_builtin(self):
        @users.groups(label="x")
        def p(ctx): ...
        assert p.resolver is _builtin_groups_resolver

    def test_users_external_empty_body_raises(self):
        with pytest.raises(ValueError, match="no built-in resolver"):
            @users.external(label="x")
            def p(ctx): ...

    def test_users_email_empty_body_raises(self):
        with pytest.raises(ValueError, match="no built-in resolver"):
            @users.email(label="x")
            def p(ctx): ...

    def test_users_pass_only_body_uses_builtin(self):
        @users(label="x")
        def p(ctx):
            pass
        assert p.resolver is _builtin_users_resolver

    def test_users_docstring_only_body_uses_builtin(self):
        @users(label="x")
        def p(ctx):
            """Just a docstring."""
        assert p.resolver is _builtin_users_resolver

    def test_users_real_body_keeps_custom_resolver(self):
        @users(label="x")
        def p(ctx):
            return [1, 2, 3]
        assert p.resolver is not _builtin_users_resolver
        # Calling it returns the list.
        assert p.resolver(None) == [1, 2, 3]

    def test_trivial_body_detected_in_exec_context(self):
        """Form files are loaded via exec(); inspect.getsource fails
        on functions defined inside exec'd code, so the trivial-body
        detection must fall back to bytecode inspection. Regression:
        before the bytecode fallback, this case ran the user's
        empty body (returning None) instead of the built-in
        resolver, producing empty picker dropdowns in production."""
        source = (
            "from frontflow import users\n"
            "\n"
            "@users(label='x')\n"
            "def p(ctx):\n"
            "    '''Empty body — should use the built-in.'''\n"
            "    ...\n"
        )
        module_globals: dict = {}
        exec(compile(source, "<test exec>", "exec"), module_globals)
        picker = module_globals["p"]
        assert picker.resolver is _builtin_users_resolver

    def test_trivial_body_pass_detected_in_exec_context(self):
        """Same regression check for `pass` bodies in exec'd code."""
        source = (
            "from frontflow import users\n"
            "@users(label='x')\n"
            "def p(ctx):\n"
            "    pass\n"
        )
        module_globals: dict = {}
        exec(compile(source, "<test exec>", "exec"), module_globals)
        assert module_globals["p"].resolver is _builtin_users_resolver

    def test_non_trivial_body_NOT_detected_in_exec_context(self):
        """Reverse check: a real function body in exec'd code must
        NOT be misclassified as trivial."""
        source = (
            "from frontflow import users\n"
            "@users(label='x')\n"
            "def p(ctx):\n"
            "    return [1, 2, 3]\n"
        )
        module_globals: dict = {}
        exec(compile(source, "<test exec>", "exec"), module_globals)
        picker = module_globals["p"]
        assert picker.resolver is not _builtin_users_resolver
        assert picker.resolver(None) == [1, 2, 3]


# ----------------------------------------------------------------------------
# Built-in resolvers query the right tables
# ----------------------------------------------------------------------------

class TestBuiltinResolvers:
    def test_builtin_users_returns_active_user_ids(
        self, app, admin_user, regular_user,
    ):
        # admin_user + regular_user fixtures both create rows.
        ids = _builtin_users_resolver(ctx=None)
        # Both active users present; values are ints.
        assert all(isinstance(i, int) for i in ids)
        assert len(ids) >= 2

    def test_builtin_users_excludes_inactive(self, app):
        # Deactivate a user and verify they're excluded.
        with Session(_engine) as s:
            user = User(
                username="picker_inactive_test",
                is_active=False,
                is_admin=False,
            )
            s.add(user)
            s.commit()
            inactive_id = user.id
        try:
            ids = _builtin_users_resolver(ctx=None)
            assert inactive_id not in ids
        finally:
            with Session(_engine) as s:
                u = s.get(User, inactive_id)
                if u is not None:
                    s.delete(u)
                    s.commit()

    def test_builtin_groups_returns_group_ids(self, app):
        # Seed at least one group.
        with Session(_engine) as s:
            g = Group(name="picker_test_group")
            s.add(g)
            s.commit()
            gid = g.id
        try:
            ids = _builtin_groups_resolver(ctx=None)
            assert gid in ids
            assert all(isinstance(i, int) for i in ids)
        finally:
            with Session(_engine) as s:
                row = s.get(Group, gid)
                if row is not None:
                    s.delete(row)
                    s.commit()


# ----------------------------------------------------------------------------
# Pickers in node bodies
# ----------------------------------------------------------------------------

class TestPickersInNodeBodies:
    def test_picker_can_be_returned_from_node(self):
        WORKFLOWS.pop("picker_form_1", None)

        @users(label="Pick a user")
        def recruiter(ctx):
            return [1, 2]

        @form(form_id="picker_form_1")
        def wf():
            @node
            def step():
                return recruiter, Button("Submit")
            step()
        wf()
        cw = compile_workflow(WORKFLOWS["picker_form_1"])
        node_cw = cw.steps[0]
        fields_by_name = {f.name: f for f in node_cw.fields}
        assert "recruiter" in fields_by_name
        assert fields_by_name["recruiter"].type == "picker"

    def test_picker_with_role_threads_through_compile(self):
        WORKFLOWS.pop("picker_form_2", None)
        approver = Role("approver")

        @users(label="Pick a user", role=approver)
        def recruiter(ctx):
            return [1]

        @form(form_id="picker_form_2")
        def wf():
            @node
            def step():
                return recruiter, Button("Submit")
            step()
        wf()
        cw = compile_workflow(WORKFLOWS["picker_form_2"])
        # Permission template picks up the role from the picker
        # input — same as any other input with role=.
        assert "approver" in cw.permission_template["roles"]
        # Field role is the identifier string.
        node_cw = cw.steps[0]
        f = node_cw.fields[0]
        assert f.role == "approver"


# ----------------------------------------------------------------------------
# PickerInput cannot be instantiated directly
# ----------------------------------------------------------------------------

class TestPickerInputBase:
    def test_direct_instantiation_raises(self):
        # PickerInput's identifier_kind is _KIND_UNSET on the base
        # class; constructing it directly should refuse.
        with pytest.raises(TypeError, match="cannot be instantiated"):
            PickerInput(resolver=lambda ctx: [], label="x")


# ----------------------------------------------------------------------------
# Resolver receives the context object
# ----------------------------------------------------------------------------

class TestResolverContext:
    def test_resolver_called_with_ctx(self):
        captured = {}

        @users(label="x")
        def p(ctx):
            captured["ctx"] = ctx
            return [1, 2]

        # Call the resolver as the render path would.
        result = p.resolver({"user": "alice", "form_id": "x"})
        assert result == [1, 2]
        # ctx flows through.
        assert captured["ctx"] == {"user": "alice", "form_id": "x"}
