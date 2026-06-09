"""Phase 1 tests for role-based access.

Covers:
  - `Role` class — identifier validation, Python-identity semantics
  - `_normalize_role_arg` — all three DSL shapes
  - `@node(role=...)` + per-input `role=` thread through to
    `CompiledNode.role` and `CompiledField.role`
  - `CompiledWorkflow.permission_template` is assembled correctly
  - Strict mode (`@form(default_role=None)`) catches missing role=
  - Duplicate role identifier check
  - `serialize_workflow` surfaces permission_template + node/field
    role into the snapshot
  - Runtime permission resolution (`resolve_node_access`,
    `resolve_field_access`)
"""
from __future__ import annotations

import pytest

from frontflow import Role, Button, form, inputs, node
from frontflow.dsl.compile import compile_workflow, serialize_workflow
from frontflow.dsl.core import (
    _DEFAULT_ROLE_NOT_SET, _normalize_role_arg, RolePermission, WORKFLOWS,
)
from frontflow.dsl.permissions import (
    FieldAccess, NodeAccess, resolve_field_access, resolve_node_access,
)


# ----------------------------------------------------------------------------
# Role class
# ----------------------------------------------------------------------------

class TestRoleSymbol:
    def test_basic_construction(self):
        r = Role("approver")
        assert r.identifier == "approver"
        assert repr(r) == "Role('approver')"

    def test_python_identity_semantics(self):
        # Two Role objects with the same identifier are still distinct
        # objects. Workflow authors are expected to share a single Role
        # object via import, not construct two.
        a = Role("approver")
        b = Role("approver")
        assert a is not b
        assert hash(a) != hash(b)

    @pytest.mark.parametrize("ident", [
        "", 42, None, "  ", "0starts_with_digit",
        "has space", "has/slash", "has#hash",
    ])
    def test_rejects_bad_identifiers(self, ident):
        with pytest.raises(ValueError):
            Role(ident)

    @pytest.mark.parametrize("ident", [
        "approver", "senior_approver", "Manager",
        "level-1", "auth.v2", "x", "A1B2C3",
    ])
    def test_accepts_valid_identifiers(self, ident):
        # Doesn't raise.
        Role(ident)


# ----------------------------------------------------------------------------
# Role normalization
# ----------------------------------------------------------------------------

class TestRoleNormalization:
    def test_single_role_means_write_only(self):
        a = Role("a")
        rp = _normalize_role_arg(a, context="x")
        assert rp.write_roles == [a]
        # Read auto-includes write.
        assert rp.read_roles == [a]

    def test_list_of_roles_all_write(self):
        a, b = Role("a"), Role("b")
        rp = _normalize_role_arg([a, b], context="x")
        assert rp.write_roles == [a, b]
        assert rp.read_roles == [a, b]

    def test_verb_dict(self):
        a, b = Role("a"), Role("b")
        rp = _normalize_role_arg(
            {"write": a, "read": b}, context="x"
        )
        assert rp.write_roles == [a]
        # b is read-only; a is added because write auto-implies read.
        assert b in rp.read_roles
        assert a in rp.read_roles

    def test_verb_dict_with_lists(self):
        a, b, c = Role("a"), Role("b"), Role("c")
        rp = _normalize_role_arg(
            {"write": [a, b], "read": [c]}, context="x"
        )
        assert rp.write_roles == [a, b]
        # All three appear in read.
        assert set(rp.read_roles) == {a, b, c}

    @pytest.mark.parametrize("bad", [
        "approver",          # string, not Role
        42,                  # int
        [Role("a"), "b"],    # list with non-Role
        {"unknown": Role("a")},  # bad verb
        {},                  # empty dict
        {"write": "string"}, # string under verb
    ])
    def test_rejects_bad_shapes(self, bad):
        with pytest.raises(ValueError):
            _normalize_role_arg(bad, context="t")

    def test_error_message_includes_context(self):
        with pytest.raises(ValueError, match="my_node"):
            _normalize_role_arg("bad", context="my_node")


# ----------------------------------------------------------------------------
# Compile output — node + field role threading
# ----------------------------------------------------------------------------

@pytest.fixture
def compile_simple_role_form():
    """Compile a fresh form on each call to avoid registry conflicts."""

    def _make(form_id: str):
        WORKFLOWS.pop(form_id, None)
        approver = Role("approver")
        monitor = Role("monitor")

        @form(form_id=form_id)
        def wf():
            @node(role=approver)
            def review():
                decision = inputs.Radio(
                    label="Decision", options=["Yes", "No"]
                )
                notes = inputs.Text(label="Notes", role=monitor)
                return decision, notes, Button("Submit")
            review()

        wf()
        return compile_workflow(WORKFLOWS[form_id]), approver, monitor

    yield _make


class TestCompileRoleThreading:
    def test_node_role_threads_to_compiled(
        self, compile_simple_role_form
    ):
        cw, approver, monitor = compile_simple_role_form("c1")
        node_cw = cw.steps[0]
        assert node_cw.role == {
            "write": ["approver"],
            "read": ["approver"],   # write auto-includes itself
        }

    def test_field_role_threads_to_compiled(
        self, compile_simple_role_form
    ):
        cw, approver, monitor = compile_simple_role_form("c2")
        node_cw = cw.steps[0]
        fields_by_name = {f.name: f for f in node_cw.fields}
        # Decision has no role= → null
        assert fields_by_name["decision"].role is None
        # Notes has role=monitor → identifier surfaced
        assert fields_by_name["notes"].role == "monitor"


# ----------------------------------------------------------------------------
# Permission template assembly
# ----------------------------------------------------------------------------

class TestPermissionTemplate:
    def test_collects_node_and_input_roles(
        self, compile_simple_role_form
    ):
        cw, approver, monitor = compile_simple_role_form("pt1")
        pt = cw.permission_template
        # Both roles appear; node-level role first (declaration order).
        assert set(pt["roles"]) == {"approver", "monitor"}
        assert pt["default_role_mode"] == "open"

    def test_no_role_declarations_yields_empty_template(self):
        WORKFLOWS.pop("pt2", None)

        @form(form_id="pt2")
        def wf():
            @node
            def step():
                x = inputs.Text(label="X")
                return x, Button("Submit")
            step()
        wf()
        cw = compile_workflow(WORKFLOWS["pt2"])
        assert cw.permission_template == {
            "roles": [],
            "default_role_mode": "open",
        }

    def test_strict_mode_default_role_mode(self):
        WORKFLOWS.pop("pt3", None)
        approver = Role("approver")

        @form(form_id="pt3", default_role=None)
        def wf():
            @node(role=approver)
            def step():
                x = inputs.Text(label="X")
                return x, Button("Submit")
            step()
        wf()
        cw = compile_workflow(WORKFLOWS["pt3"])
        assert cw.permission_template["default_role_mode"] == "strict"


# ----------------------------------------------------------------------------
# Strict-mode validation
# ----------------------------------------------------------------------------

class TestStrictMode:
    def test_strict_mode_rejects_node_without_role(self):
        WORKFLOWS.pop("strict_bad", None)

        @form(form_id="strict_bad", default_role=None)
        def wf():
            @node  # no role= — compile error expected
            def step():
                x = inputs.Text(label="X")
                return x, Button("Submit")
            step()
        wf()
        with pytest.raises(ValueError, match="strict mode"):
            compile_workflow(WORKFLOWS["strict_bad"])

    def test_open_mode_permits_node_without_role(self):
        # Sanity: same form without strict mode compiles fine.
        WORKFLOWS.pop("open_ok", None)

        @form(form_id="open_ok")
        def wf():
            @node
            def step():
                x = inputs.Text(label="X")
                return x, Button("Submit")
            step()
        wf()
        # Doesn't raise.
        compile_workflow(WORKFLOWS["open_ok"])


# ----------------------------------------------------------------------------
# Duplicate-identifier check
# ----------------------------------------------------------------------------

class TestDuplicateRoles:
    def test_two_different_role_objects_same_identifier_rejected(self):
        WORKFLOWS.pop("dup", None)
        a1 = Role("approver")
        a2 = Role("approver")  # different object, same identifier

        @form(form_id="dup")
        def wf():
            @node(role=a1)
            def step():
                x = inputs.Text(label="X", role=a2)
                return x, Button("Submit")
            step()
        wf()
        with pytest.raises(
            ValueError,
            match="two different Role objects",
        ):
            compile_workflow(WORKFLOWS["dup"])

    def test_same_role_object_reused_is_fine(self):
        # Workflow author shares one Role object across references.
        WORKFLOWS.pop("shared", None)
        a = Role("approver")

        @form(form_id="shared")
        def wf():
            @node(role=a)
            def step():
                x = inputs.Text(label="X", role=a)
                return x, Button("Submit")
            step()
        wf()
        cw = compile_workflow(WORKFLOWS["shared"])
        # One role identifier; no error.
        assert cw.permission_template["roles"] == ["approver"]


# ----------------------------------------------------------------------------
# Serialization
# ----------------------------------------------------------------------------

class TestSerialization:
    def test_serialized_carries_permission_template(
        self, compile_simple_role_form
    ):
        cw, _, _ = compile_simple_role_form("ser1")
        s = serialize_workflow(cw)
        assert "permission_template" in s
        assert set(s["permission_template"]["roles"]) == {
            "approver", "monitor",
        }
        assert s["permission_template"]["default_role_mode"] == "open"

    def test_serialized_node_carries_role(
        self, compile_simple_role_form
    ):
        cw, _, _ = compile_simple_role_form("ser2")
        s = serialize_workflow(cw)
        assert s["steps"][0]["role"] == {
            "write": ["approver"], "read": ["approver"],
        }

    def test_serialized_fields_carry_role(
        self, compile_simple_role_form
    ):
        cw, _, _ = compile_simple_role_form("ser3")
        s = serialize_workflow(cw)
        fields = s["steps"][0]["fields"]
        roles_by_name = {f["name"]: f["role"] for f in fields}
        assert roles_by_name["decision"] is None
        assert roles_by_name["notes"] == "monitor"

    def test_serialized_unroled_node_role_is_null(self):
        WORKFLOWS.pop("ser_unroled", None)

        @form(form_id="ser_unroled")
        def wf():
            @node
            def step():
                x = inputs.Text(label="X")
                return x, Button("Submit")
            step()
        wf()
        cw = compile_workflow(WORKFLOWS["ser_unroled"])
        s = serialize_workflow(cw)
        assert s["steps"][0]["role"] is None
        assert s["steps"][0]["fields"][0]["role"] is None


# ----------------------------------------------------------------------------
# Runtime permission resolution
# ----------------------------------------------------------------------------

class TestNodeAccess:
    def test_no_role_open_mode_grants_full(self):
        # node_role=None, default_role_mode=open → full access.
        a = resolve_node_access(
            node_role=None, default_role_mode="open",
            user_roles_on_submission=frozenset(), is_admin=False,
        )
        assert a == NodeAccess(can_read=True, can_write=True)

    def test_no_role_strict_mode_denies(self):
        # node_role=None, default_role_mode=strict → defensive deny.
        # (In practice strict mode would have failed compile, but
        # the runtime should still fail closed on a stale snapshot.)
        a = resolve_node_access(
            node_role=None, default_role_mode="strict",
            user_roles_on_submission=frozenset(), is_admin=False,
        )
        assert a.can_read is False
        assert a.can_write is False

    def test_admin_always_full_access(self):
        a = resolve_node_access(
            node_role={"write": ["approver"], "read": ["approver"]},
            default_role_mode="open",
            user_roles_on_submission=frozenset(),
            is_admin=True,
        )
        assert a == NodeAccess(can_read=True, can_write=True)

    def test_user_with_write_role_gets_both(self):
        a = resolve_node_access(
            node_role={"write": ["approver"], "read": ["approver"]},
            default_role_mode="open",
            user_roles_on_submission=frozenset({"approver"}),
            is_admin=False,
        )
        assert a.can_write is True
        assert a.can_read is True
        assert a.pending is False

    def test_user_with_read_role_only(self):
        a = resolve_node_access(
            node_role={"write": ["approver"], "read": ["approver", "monitor"]},
            default_role_mode="open",
            user_roles_on_submission=frozenset({"monitor"}),
            is_admin=False,
        )
        assert a.can_read is True
        assert a.can_write is False
        assert a.pending is False

    def test_user_with_no_matching_role_renders_pending(self):
        a = resolve_node_access(
            node_role={"write": ["approver"], "read": ["approver"]},
            default_role_mode="open",
            user_roles_on_submission=frozenset({"unrelated"}),
            is_admin=False,
        )
        assert a.can_read is False
        assert a.can_write is False
        assert a.pending is True
        assert a.missing_write_roles == ("approver",)


class TestFieldAccess:
    def test_admin_full_field(self):
        node_acc = NodeAccess(can_read=True, can_write=True)
        f = resolve_field_access(
            field_role="approver", node_access=node_acc,
            user_roles_on_submission=frozenset(),
            is_admin=True,
        )
        assert f == FieldAccess(can_read=True, can_write=True)

    def test_no_node_read_blocks_field(self):
        node_acc = NodeAccess(can_read=False, can_write=False)
        f = resolve_field_access(
            field_role=None, node_access=node_acc,
            user_roles_on_submission=frozenset(),
            is_admin=False,
        )
        assert f.can_read is False

    def test_no_field_role_follows_node_write(self):
        node_acc = NodeAccess(can_read=True, can_write=True)
        f = resolve_field_access(
            field_role=None, node_access=node_acc,
            user_roles_on_submission=frozenset(),
            is_admin=False,
        )
        assert f.can_write is True
        assert f.can_read is True

    def test_field_role_held_grants_write(self):
        node_acc = NodeAccess(can_read=True, can_write=True)
        f = resolve_field_access(
            field_role="approver",
            node_access=node_acc,
            user_roles_on_submission=frozenset({"approver"}),
            is_admin=False,
        )
        assert f.can_write is True

    def test_field_role_missing_renders_pending(self):
        # User has node write (via some other role) but not the
        # field's role → field renders pending (label visible,
        # input disabled).
        node_acc = NodeAccess(can_read=True, can_write=True)
        f = resolve_field_access(
            field_role="approver",
            node_access=node_acc,
            user_roles_on_submission=frozenset({"other_role"}),
            is_admin=False,
        )
        assert f.can_read is True
        assert f.can_write is False
        assert f.pending is True

    def test_no_node_write_with_field_role_still_no_write(self):
        # Read-only on the node → field can't be written, even if
        # the user holds the field's role. Field role narrows write,
        # never broadens it.
        node_acc = NodeAccess(can_read=True, can_write=False)
        f = resolve_field_access(
            field_role="approver",
            node_access=node_acc,
            user_roles_on_submission=frozenset({"approver"}),
            is_admin=False,
        )
        assert f.can_write is False
        assert f.can_read is True


# ----------------------------------------------------------------------------
# Form-level role= declaration (Phase 7a)
# ----------------------------------------------------------------------------
#
# `@form(role=...)` provides a default RolePermission that nodes
# inherit when they have no explicit `role=`. Explicit node-level
# role= overrides completely (no merging). Authoring win: a form
# with one reader role and one writer role doesn't repeat the
# declaration on every node.


class TestFormLevelRole:
    def teardown_method(self) -> None:
        for fid in ("flr_single", "flr_dict", "flr_override", "flr_inherited"):
            WORKFLOWS.pop(fid, None)

    def test_form_role_with_single_role_inherits_on_nodes(self):
        """`@form(role=writer)` — every node inherits as write-only."""
        writer = Role("writer")

        @form(form_id="flr_single", role=writer)
        def flr_single():
            @node
            def step1():
                txt = inputs.Text(label="x")
                return txt, Button("Next")
            @node
            def step2():
                txt = inputs.Text(label="y")
                return txt, Button("Submit")
            step1() >> step2()
        flr_single()

        cw = compile_workflow(WORKFLOWS["flr_single"])
        for step in cw.steps:
            assert step.role is not None, f"node {step.id} did not inherit form role"
            assert step.role["write"] == ["writer"]
            # Write implies read.
            assert "writer" in step.role["read"]

    def test_form_role_with_dict_inherits_split_read_write(self):
        """`@form(role={"read": reader, "write": writer})` — every
        node inherits both as read-only-for-reader, write-for-writer."""
        reader = Role("reader")
        writer = Role("writer")

        @form(form_id="flr_dict", role={"read": reader, "write": writer})
        def flr_dict():
            @node
            def step1():
                txt = inputs.Text(label="x")
                return txt, Button("Submit")
            step1()
        flr_dict()

        cw = compile_workflow(WORKFLOWS["flr_dict"])
        step = cw.steps[0]
        assert step.role is not None
        assert step.role["write"] == ["writer"]
        # Reader can read (explicit), writer can read (auto from write).
        assert set(step.role["read"]) == {"reader", "writer"}

    def test_node_role_overrides_form_role(self):
        """Explicit `@node(role=...)` wins over form-level role —
        no merging."""
        reader = Role("reader")
        writer = Role("writer")
        approver = Role("approver")

        @form(form_id="flr_override", role={"read": reader, "write": writer})
        def flr_override():
            @node(role=approver)
            def step1():
                txt = inputs.Text(label="x")
                return txt, Button("Submit")
            step1()
        flr_override()

        cw = compile_workflow(WORKFLOWS["flr_override"])
        step = cw.steps[0]
        # The form-level reader/writer are GONE from this node — only
        # the approver role applies (write-only, with auto-read).
        assert step.role is not None
        assert step.role["write"] == ["approver"]
        assert set(step.role["read"]) == {"approver"}
        assert "reader" not in step.role["read"]
        assert "writer" not in step.role["write"]

    def test_form_role_satisfies_strict_mode(self):
        """`@form(role=..., default_role=None)` — strict mode is
        satisfied for every node because the form-level role
        provides a default. Without form-level role, strict mode
        would error on the un-declared node."""
        writer = Role("writer")

        @form(
            form_id="flr_inherited", role=writer, default_role=None,
        )
        def flr_inherited():
            @node
            def step1():
                txt = inputs.Text(label="x")
                return txt, Button("Submit")
            step1()
        flr_inherited()

        # Should compile without raising — form-level role covers
        # the strict-mode requirement.
        cw = compile_workflow(WORKFLOWS["flr_inherited"])
        assert cw.steps[0].role is not None

    def test_form_role_registered_in_permission_template(self):
        """A form-level role appears in permission_template['roles']
        even when no node references it explicitly — otherwise the
        introspection surface would lie about which roles exist."""
        reader = Role("reader")
        writer = Role("writer")

        @form(form_id="flr_dict", role={"read": reader, "write": writer})
        def flr_dict():
            @node
            def step1():
                txt = inputs.Text(label="x")
                return txt, Button("Submit")
            step1()
        flr_dict()

        cw = compile_workflow(WORKFLOWS["flr_dict"])
        assert set(cw.permission_template["roles"]) == {"reader", "writer"}

    def test_form_role_with_bad_shape_raises_at_decoration(self):
        """Authoring errors surface at @form decoration, not at compile."""
        with pytest.raises(ValueError, match=r"role.*"):
            form(
                role="not a role",
                form_id="flr_bad",
            )(lambda: None)


class TestFormLevelDefaultRole:
    """Phase-15 / likely-next-ask #4: `@form(default_role=...)` sets
    the inheritance default for nodes without their own `role=`,
    without taking on the semantics of `role=` (which conceptually
    belongs to "the form itself"). When both `role=` and
    `default_role=` are set, `default_role` wins for node
    inheritance — it's the more specific kwarg.
    """

    def teardown_method(self) -> None:
        for fid in (
            "fdr_single", "fdr_dict", "fdr_override", "fdr_wins",
            "fdr_bad", "fdr_role_only", "fdr_strict",
        ):
            WORKFLOWS.pop(fid, None)

    def test_default_role_with_single_role_inherits_on_nodes(self):
        """`@form(default_role=writer)` — every node inherits as
        write-only, identical to `role=writer` for inheritance
        purposes but using the kwarg that names the intent
        ('default for inheritance') rather than 'form's role'."""
        writer = Role("writer")

        @form(form_id="fdr_single", default_role=writer)
        def fdr_single():
            @node
            def step1():
                txt = inputs.Text(label="x")
                return txt, Button("Submit")
            step1()
        fdr_single()

        cw = compile_workflow(WORKFLOWS["fdr_single"])
        step = cw.steps[0]
        assert step.role is not None
        assert step.role["write"] == ["writer"]

    def test_default_role_with_dict_inherits_split_read_write(self):
        """`@form(default_role={"read": reader, "write": writer})`
        — same split-permission semantics as `role={"read":...,
        "write":...}`."""
        reader = Role("reader")
        writer = Role("writer")

        @form(
            form_id="fdr_dict",
            default_role={"read": reader, "write": writer},
        )
        def fdr_dict():
            @node
            def step1():
                txt = inputs.Text(label="x")
                return txt, Button("Submit")
            step1()
        fdr_dict()

        cw = compile_workflow(WORKFLOWS["fdr_dict"])
        step = cw.steps[0]
        assert step.role is not None
        assert step.role["write"] == ["writer"]
        assert set(step.role["read"]) == {"reader", "writer"}

    def test_node_role_overrides_default_role(self):
        """Explicit `@node(role=...)` wins over `default_role=` —
        same override rule as `role=`."""
        reader = Role("reader")
        approver = Role("approver")

        @form(form_id="fdr_override", default_role=reader)
        def fdr_override():
            @node(role=approver)
            def step1():
                txt = inputs.Text(label="x")
                return txt, Button("Submit")
            step1()
        fdr_override()

        cw = compile_workflow(WORKFLOWS["fdr_override"])
        step = cw.steps[0]
        assert step.role["write"] == ["approver"]
        # The form-level default does NOT bleed in.
        assert "reader" not in step.role["write"]

    def test_default_role_wins_over_role_for_inheritance(self):
        """When BOTH `role=` and `default_role=` are set on the
        form, `default_role` wins for node inheritance — the more
        specific kwarg always overrides."""
        form_role = Role("form_role")
        inherited = Role("inherited")

        @form(
            form_id="fdr_wins",
            role=form_role,
            default_role=inherited,
        )
        def fdr_wins():
            @node
            def step1():
                txt = inputs.Text(label="x")
                return txt, Button("Submit")
            step1()
        fdr_wins()

        cw = compile_workflow(WORKFLOWS["fdr_wins"])
        step = cw.steps[0]
        # Node inherited 'inherited', not 'form_role'.
        assert step.role["write"] == ["inherited"]

    def test_default_role_with_bad_shape_raises_at_decoration(self):
        """Authoring errors surface at @form decoration, not at
        compile — same eager-validation policy as `role=`."""
        with pytest.raises(
            ValueError, match=r"default_role",
        ):
            form(
                default_role="not a role",
                form_id="fdr_bad",
            )(lambda: None)

    def test_role_alone_still_works_unchanged(self):
        """Regression: passing only `role=` continues to drive
        inheritance, so the existing usage in the wild keeps
        working."""
        writer = Role("writer")

        @form(form_id="fdr_role_only", role=writer)
        def fdr_role_only():
            @node
            def step1():
                txt = inputs.Text(label="x")
                return txt, Button("Submit")
            step1()
        fdr_role_only()

        cw = compile_workflow(WORKFLOWS["fdr_role_only"])
        step = cw.steps[0]
        assert step.role["write"] == ["writer"]

    def test_default_role_none_remains_strict_mode(self):
        """`default_role=None` continues to mean strict mode: a node
        without its own role= is a compile-time error. Asserts the
        new RolePermission accepting branch doesn't accidentally
        collapse None onto "open"."""
        @form(form_id="fdr_strict", default_role=None)
        def fdr_strict():
            @node
            def step1():
                txt = inputs.Text(label="x")
                return txt, Button("Submit")
            step1()
        fdr_strict()

        with pytest.raises(ValueError, match=r"strict|role"):
            compile_workflow(WORKFLOWS["fdr_strict"])
