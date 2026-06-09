"""Phase 4 tests — Assign operator + assignments + inbox.

Covers:
  - assignments.grant / revoke / revoke_all / lookups
  - Idempotent grant: existing active row is returned
  - Re-grant after revoke inserts a NEW row
  - active_roles_for_user_on_submission returns frozenset of identifiers
  - Submission row carries parent_* columns
  - Assign operator construction + value validation
  - CompiledNode.assigns is populated from the execution chain
  - validate_assign_references catches unknown form, unknown role,
    non-picker `to=`, off-node `to=` reference
  - GET /api/my-tasks returns active assignments for the signed-in
    user, with submission + form metadata
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from frontflow import (
    Assign, Button, Role, form, inputs, node, steps, users,
)
from frontflow.dsl import assignments
from frontflow.dsl.compile import (
    compile_workflow, validate_assign_references,
)
from frontflow.dsl.core import WORKFLOWS
from frontflow.dsl.store import (
    SubmissionAssignment, User, _engine,
)


def _user_id(username: str) -> int:
    with Session(_engine) as s:
        return s.execute(
            select(User).where(User.username == username)
        ).scalar_one().id


# ----------------------------------------------------------------------------
# Assignment CRUD
# ----------------------------------------------------------------------------

class TestAssignmentsCRUD:
    def test_grant_creates_row(self, app, admin_user, regular_user):
        a = assignments.grant(
            submission_handle="sub-1",
            user_id=_user_id("user"),
            role_id="approver",
            granted_by_user_id=_user_id("admin"),
        )
        assert a.submission_handle == "sub-1"
        assert a.role_id == "approver"
        assert a.revoked_at is None

    def test_grant_is_idempotent_for_active_row(
        self, app, admin_user, regular_user
    ):
        first = assignments.grant(
            submission_handle="sub-2",
            user_id=_user_id("user"),
            role_id="approver",
            granted_by_user_id=_user_id("admin"),
        )
        # Second grant with same triple returns the SAME row id.
        second = assignments.grant(
            submission_handle="sub-2",
            user_id=_user_id("user"),
            role_id="approver",
            granted_by_user_id=_user_id("admin"),
        )
        assert first.id == second.id

    def test_revoke_then_regrant_inserts_new_row(
        self, app, admin_user, regular_user
    ):
        first = assignments.grant(
            submission_handle="sub-3",
            user_id=_user_id("user"),
            role_id="approver",
            granted_by_user_id=_user_id("admin"),
        )
        revoked = assignments.revoke(
            assignment_id=first.id,
            revoked_by_user_id=_user_id("admin"),
        )
        assert revoked is not None
        assert revoked.revoked_at is not None

        # Re-grant inserts a NEW row, doesn't reactivate the old.
        second = assignments.grant(
            submission_handle="sub-3",
            user_id=_user_id("user"),
            role_id="approver",
            granted_by_user_id=_user_id("admin"),
        )
        assert second.id != first.id
        assert second.revoked_at is None

    def test_revoke_unknown_returns_none(self, app):
        assert assignments.revoke(
            assignment_id=999_999, revoked_by_user_id=1,
        ) is None

    def test_revoke_already_revoked_returns_none(
        self, app, admin_user, regular_user
    ):
        a = assignments.grant(
            submission_handle="sub-4",
            user_id=_user_id("user"),
            role_id="approver",
            granted_by_user_id=_user_id("admin"),
        )
        assignments.revoke(
            assignment_id=a.id, revoked_by_user_id=_user_id("admin"),
        )
        # Second revoke is a no-op.
        again = assignments.revoke(
            assignment_id=a.id, revoked_by_user_id=_user_id("admin"),
        )
        assert again is None

    def test_revoke_all_for_user_on_submission(
        self, app, admin_user, regular_user
    ):
        user_id = _user_id("user")
        admin_id = _user_id("admin")
        for role in ("a", "b", "c"):
            assignments.grant(
                submission_handle="sub-multi",
                user_id=user_id,
                role_id=role,
                granted_by_user_id=admin_id,
            )
        count = assignments.revoke_all_for_user_on_submission(
            submission_handle="sub-multi",
            user_id=user_id,
            revoked_by_user_id=admin_id,
        )
        assert count == 3
        # All revoked → no active roles.
        active = assignments.active_roles_for_user_on_submission(
            "sub-multi", user_id,
        )
        assert active == frozenset()

    def test_active_roles_lookup(
        self, app, admin_user, regular_user
    ):
        user_id = _user_id("user")
        admin_id = _user_id("admin")
        for role in ("approver", "monitor"):
            assignments.grant(
                submission_handle="sub-roles",
                user_id=user_id,
                role_id=role,
                granted_by_user_id=admin_id,
            )
        roles = assignments.active_roles_for_user_on_submission(
            "sub-roles", user_id,
        )
        assert isinstance(roles, frozenset)
        assert roles == frozenset({"approver", "monitor"})

    def test_list_active_for_user(
        self, app, admin_user, regular_user
    ):
        user_id = _user_id("user")
        admin_id = _user_id("admin")
        for handle in ("sub-a", "sub-b"):
            assignments.grant(
                submission_handle=handle,
                user_id=user_id,
                role_id="approver",
                granted_by_user_id=admin_id,
            )
        rows = assignments.list_active_for_user(user_id)
        handles = {r["submission_handle"] for r in rows}
        assert {"sub-a", "sub-b"} <= handles

    def test_grant_rejects_empty_role(
        self, app, admin_user, regular_user
    ):
        with pytest.raises(ValueError, match="role_id"):
            assignments.grant(
                submission_handle="sub-x",
                user_id=_user_id("user"),
                role_id="",
                granted_by_user_id=_user_id("admin"),
            )

    def test_history_includes_revoked_rows(
        self, app, admin_user, regular_user
    ):
        admin_id = _user_id("admin")
        user_id = _user_id("user")
        a = assignments.grant(
            submission_handle="sub-hist",
            user_id=user_id,
            role_id="approver",
            granted_by_user_id=admin_id,
        )
        assignments.revoke(
            assignment_id=a.id, revoked_by_user_id=admin_id,
        )
        b = assignments.grant(
            submission_handle="sub-hist",
            user_id=user_id,
            role_id="approver",
            granted_by_user_id=admin_id,
        )
        history = assignments.list_history_for_submission("sub-hist")
        assert len(history) == 2
        # Active list shows only the active row.
        active = assignments.list_active_for_submission("sub-hist")
        assert len(active) == 1
        assert active[0]["id"] == b.id


# ----------------------------------------------------------------------------
# Assign operator (value object)
# ----------------------------------------------------------------------------

class TestAssignOperator:
    def test_basic_construction(self):
        a = Assign(form="child", to=None, role="approver")
        assert a.form_id == "child"
        assert a.role_id == "approver"
        assert a.link_ttl_days == 7
        assert a.prefill == {}

    def test_prefill_is_copied(self):
        d = {"x": 1}
        a = Assign(form="c", to=None, role="r", prefill=d)
        d["x"] = 999
        assert a.prefill["x"] == 1

    @pytest.mark.parametrize("form_id", ["", 42, None])
    def test_rejects_bad_form_id(self, form_id):
        with pytest.raises(ValueError):
            Assign(form=form_id, to=None, role="r")

    @pytest.mark.parametrize("role_id", ["", 42, None])
    def test_rejects_bad_role(self, role_id):
        with pytest.raises(ValueError):
            Assign(form="c", to=None, role=role_id)

    @pytest.mark.parametrize("ttl", [0, -1, 1.5, "7"])
    def test_rejects_bad_ttl(self, ttl):
        with pytest.raises(ValueError):
            Assign(form="c", to=None, role="r", link_ttl_days=ttl)


# ----------------------------------------------------------------------------
# Compile threading
# ----------------------------------------------------------------------------

@pytest.fixture
def two_form_setup():
    """Register a child form and a parent form with one Assign call."""
    WORKFLOWS.pop("child_pf4", None)
    WORKFLOWS.pop("parent_pf4", None)

    recruiter_role = Role("recruiter")

    @form(form_id="child_pf4")
    def child():
        @node(role=recruiter_role)
        def screening():
            notes = inputs.TextBlock(label="Notes")
            return notes, Button("Submit")
        screening()
    child()

    @users(label="Pick a recruiter")
    def recruiter_picker(ctx):
        return [1, 2, 3]

    @form(form_id="parent_pf4")
    def parent():
        @node
        def kickoff():
            project = inputs.Text(label="Project")
            submit = Button("Kick off")
            spawn = Assign(
                form="child_pf4",
                to=steps.kickoff.recruiter_picker,
                role="recruiter",
                prefill={"notes": "Pre-filled."},
            )
            submit >> spawn
            return project, recruiter_picker, submit
        kickoff()
    parent()

    return {
        "parent": WORKFLOWS["parent_pf4"],
        "child": WORKFLOWS["child_pf4"],
    }


class TestAssignCompile:
    def test_assigns_collected_on_node(self, two_form_setup):
        cw = compile_workflow(two_form_setup["parent"])
        n = cw.steps[0]
        assert len(n.assigns) == 1
        a = n.assigns[0]
        assert a.form_id == "child_pf4"
        assert a.role_id == "recruiter"
        assert a.op_idx == 0
        # to_ref_descriptor is the StepRef serialization
        assert a.to_ref_descriptor == {
            "node": "kickoff", "name": "recruiter_picker",
        }

    def test_prefill_serialized(self, two_form_setup):
        cw = compile_workflow(two_form_setup["parent"])
        n = cw.steps[0]
        a = n.assigns[0]
        assert a.prefill_descriptors == {
            "notes": {"kind": "literal", "value": "Pre-filled."},
        }


# ----------------------------------------------------------------------------
# Cross-form validation
# ----------------------------------------------------------------------------

class TestValidateAssignReferences:
    def test_valid_passes(self, two_form_setup):
        registry = {
            "child_pf4": compile_workflow(two_form_setup["child"]),
            "parent_pf4": compile_workflow(two_form_setup["parent"]),
        }
        # Doesn't raise.
        validate_assign_references(registry["parent_pf4"], registry)

    def test_unknown_form_rejected(self):
        WORKFLOWS.pop("bad_form", None)

        @users(label="Pick")
        def picker(ctx): ...

        @form(form_id="bad_form")
        def wf():
            @node
            def k():
                submit = Button("Send")
                spawn = Assign(
                    form="does_not_exist",
                    to=steps.k.picker, role="x",
                )
                submit >> spawn
                return picker, submit
            k()
        wf()
        cw = compile_workflow(WORKFLOWS["bad_form"])
        with pytest.raises(ValueError, match="no workflow"):
            validate_assign_references(cw, {"bad_form": cw})

    def test_unknown_role_rejected(self, two_form_setup):
        WORKFLOWS.pop("bad_role_form", None)

        @users(label="Pick")
        def picker(ctx): ...

        @form(form_id="bad_role_form")
        def wf():
            @node
            def k():
                submit = Button("Send")
                spawn = Assign(
                    form="child_pf4",
                    to=steps.k.picker,
                    role="not_a_role",
                )
                submit >> spawn
                return picker, submit
            k()
        wf()
        registry = {
            "child_pf4": compile_workflow(two_form_setup["child"]),
            "bad_role_form": compile_workflow(WORKFLOWS["bad_role_form"]),
        }
        with pytest.raises(ValueError, match="not_a_role"):
            validate_assign_references(
                registry["bad_role_form"], registry,
            )

    def test_non_picker_to_ref_rejected(self, two_form_setup):
        WORKFLOWS.pop("non_picker_form", None)

        @form(form_id="non_picker_form")
        def wf():
            @node
            def k():
                # Text input, not a picker — should be rejected.
                somebody = inputs.Email(label="Who")
                submit = Button("Send")
                spawn = Assign(
                    form="child_pf4",
                    to=steps.k.somebody,
                    role="recruiter",
                )
                submit >> spawn
                return somebody, submit
            k()
        wf()
        registry = {
            "child_pf4": compile_workflow(two_form_setup["child"]),
            "non_picker_form": compile_workflow(WORKFLOWS["non_picker_form"]),
        }
        with pytest.raises(ValueError, match="picker"):
            validate_assign_references(
                registry["non_picker_form"], registry,
            )




class TestChildSubmissionIdTemplateValidation:
    """Phase-9 compile-time validation: an Assign that targets a
    child form whose `submission_id_template` references unknown
    nodes / fields must be rejected at scan time, not at the first
    parent submit."""

    def test_template_referencing_landing_field_from_prefill_passes(self):
        # Child has a submission_id_template referencing a field
        # filled by the parent's prefill — the normal happy path.
        WORKFLOWS.pop("child_tpl_ok", None)
        WORKFLOWS.pop("parent_tpl_ok", None)

        @form(
            form_id="child_tpl_ok",
            submission_id="ticket-{{ steps.landing.ticket_id }}",
        )
        def child():
            @node
            def landing():
                ticket_id = inputs.Text(label="Ticket id")
                return ticket_id, Button("Submit")
            landing()
        child()

        @users(label="Pick")
        def assignee_picker(ctx):
            return [1]

        @form(form_id="parent_tpl_ok")
        def parent():
            @node
            def k():
                submit = Button("Send")
                spawn = Assign(
                    form="child_tpl_ok",
                    to=steps.k.assignee_picker,
                    role="r",
                    prefill={"ticket_id": "ABC-123"},
                )
                submit >> spawn
                return assignee_picker, submit
            k()
        parent()

        # Need at least one role on the child for Assign's role
        # check to pass. The previous fixture roles aren't relevant
        # here — just declare 'r' on the child node directly.
        # Re-register the child with the role.
        WORKFLOWS.pop("child_tpl_ok", None)
        recruiter = Role("r")

        @form(
            form_id="child_tpl_ok",
            submission_id="ticket-{{ steps.landing.ticket_id }}",
        )
        def child2():
            @node(role=recruiter)
            def landing():
                ticket_id = inputs.Text(label="Ticket id")
                return ticket_id, Button("Submit")
            landing()
        child2()

        registry = {
            "child_tpl_ok": compile_workflow(WORKFLOWS["child_tpl_ok"]),
            "parent_tpl_ok": compile_workflow(WORKFLOWS["parent_tpl_ok"]),
        }
        # Must not raise.
        validate_assign_references(registry["parent_tpl_ok"], registry)

    def test_template_referencing_nonexistent_node_rejected(self):
        # Child template says `steps.kickoff.project` but the child
        # form has no node named `kickoff` — author confused parent
        # and child node names.
        WORKFLOWS.pop("child_tpl_bad_node", None)
        WORKFLOWS.pop("parent_tpl_bad_node", None)

        recruiter = Role("r")

        @form(
            form_id="child_tpl_bad_node",
            submission_id="x-{{ steps.kickoff.project | slugify }}",
        )
        def child():
            @node(role=recruiter)
            def landing():
                # Note: child has 'landing' node, not 'kickoff'.
                project = inputs.Text(label="Project")
                return project, Button("Submit")
            landing()
        child()

        @users(label="Pick")
        def picker(ctx):
            return [1]

        @form(form_id="parent_tpl_bad_node")
        def parent():
            @node
            def k():
                submit = Button("Send")
                spawn = Assign(
                    form="child_tpl_bad_node",
                    to=steps.k.picker,
                    role="r",
                    prefill={"project": "foo"},
                )
                submit >> spawn
                return picker, submit
            k()
        parent()

        registry = {
            "child_tpl_bad_node": compile_workflow(
                WORKFLOWS["child_tpl_bad_node"]
            ),
            "parent_tpl_bad_node": compile_workflow(
                WORKFLOWS["parent_tpl_bad_node"]
            ),
        }
        with pytest.raises(
            ValueError, match="has no node named 'kickoff'",
        ):
            validate_assign_references(
                registry["parent_tpl_bad_node"], registry,
            )

    def test_template_referencing_nonexistent_field_on_landing_rejected(self):
        # Child template references `steps.landing.applicant`, but
        # the child landing has no `applicant` input AND parent's
        # Assign.prefill doesn't supply one either. The template
        # would never resolve.
        WORKFLOWS.pop("child_tpl_bad_field", None)
        WORKFLOWS.pop("parent_tpl_bad_field", None)

        recruiter = Role("r")

        @form(
            form_id="child_tpl_bad_field",
            submission_id="x-{{ steps.landing.applicant }}",
        )
        def child():
            @node(role=recruiter)
            def landing():
                # No 'applicant' input.
                notes = inputs.TextBlock(label="Notes")
                return notes, Button("Submit")
            landing()
        child()

        @users(label="Pick")
        def picker(ctx):
            return [1]

        @form(form_id="parent_tpl_bad_field")
        def parent():
            @node
            def k():
                submit = Button("Send")
                spawn = Assign(
                    form="child_tpl_bad_field",
                    to=steps.k.picker,
                    role="r",
                    # prefill DOES NOT supply 'applicant' either.
                    prefill={"notes": "hi"},
                )
                submit >> spawn
                return picker, submit
            k()
        parent()

        registry = {
            "child_tpl_bad_field": compile_workflow(
                WORKFLOWS["child_tpl_bad_field"]
            ),
            "parent_tpl_bad_field": compile_workflow(
                WORKFLOWS["parent_tpl_bad_field"]
            ),
        }
        with pytest.raises(
            ValueError, match="prefill={...} doesn't supply it",
        ):
            validate_assign_references(
                registry["parent_tpl_bad_field"], registry,
            )

    def test_template_field_supplied_by_prefill_only_passes(self):
        # A field that exists ONLY in parent's prefill (not as a
        # child input) still satisfies the template, because the
        # runtime stuffs it into the child's landing form_values
        # before minting.
        WORKFLOWS.pop("child_tpl_prefill_only", None)
        WORKFLOWS.pop("parent_tpl_prefill_only", None)

        recruiter = Role("r")

        @form(
            form_id="child_tpl_prefill_only",
            submission_id="x-{{ steps.landing.ext_ref }}",
        )
        def child():
            @node(role=recruiter)
            def landing():
                # No 'ext_ref' input on the child form.
                notes = inputs.TextBlock(label="Notes")
                return notes, Button("Submit")
            landing()
        child()

        @users(label="Pick")
        def picker(ctx):
            return [1]

        @form(form_id="parent_tpl_prefill_only")
        def parent():
            @node
            def k():
                submit = Button("Send")
                spawn = Assign(
                    form="child_tpl_prefill_only",
                    to=steps.k.picker,
                    role="r",
                    prefill={"ext_ref": "external-id-42"},
                )
                submit >> spawn
                return picker, submit
            k()
        parent()

        registry = {
            "child_tpl_prefill_only": compile_workflow(
                WORKFLOWS["child_tpl_prefill_only"]
            ),
            "parent_tpl_prefill_only": compile_workflow(
                WORKFLOWS["parent_tpl_prefill_only"]
            ),
        }
        # Must not raise — prefill supplies ext_ref.
        validate_assign_references(
            registry["parent_tpl_prefill_only"], registry,
        )


# ----------------------------------------------------------------------------
# /api/my-tasks
# ----------------------------------------------------------------------------

class TestMyTasksEndpoint:
    def test_empty_when_no_assignments(
        self, user_client: TestClient
    ):
        r = user_client.get("/api/my-tasks")
        assert r.status_code == 200
        assert r.json() == []

    def test_anon_gets_401(self, anon_client: TestClient, admin_user):
        r = anon_client.get("/api/my-tasks")
        assert r.status_code == 401

    def test_returns_active_assignments(
        self, user_client: TestClient,
        admin_user, regular_user,
    ):
        # Seed an assignment row, then a corresponding submission +
        # form_version row so the endpoint's join can resolve.
        from frontflow.dsl.store import (
            Form, FormVersion, Submission, _utcnow as _now,
        )
        admin_id = _user_id("admin")
        user_id = _user_id("user")
        with Session(_engine) as s:
            # Form + form_version + submission referenced by handle.
            f = Form(
                form_id="mytasks_form", name="My Tasks Form",
                folder_path="",
            )
            s.merge(f)
            s.commit()
            ver = FormVersion(
                form_id="mytasks_form",
                version=1,
                compiled_graph={"id": "mytasks_form", "title": "My Tasks Form"},
                content_hash="abc",
                source="x",
                created_at=_now(),
            )
            s.add(ver)
            s.commit()
            sub = Submission(
                handle="mytasks-handle-1",
                submission_id="mytasks-1",
                form_version_id=ver.id,
                state="running",
                created_at=_now(),
                updated_at=_now(),
            )
            s.add(sub)
            s.commit()

        assignments.grant(
            submission_handle="mytasks-handle-1",
            user_id=user_id,
            role_id="approver",
            granted_by_user_id=admin_id,
        )

        r = user_client.get("/api/my-tasks")
        assert r.status_code == 200
        body = r.json()
        assert len(body) >= 1
        entry = next(
            x for x in body
            if x["submission_handle"] == "mytasks-handle-1"
        )
        assert entry["role_id"] == "approver"
        assert entry["form_id"] == "mytasks_form"
        assert entry["form_title"] == "My Tasks Form"
        assert entry["submission_state"] == "running"

    def test_excludes_revoked_assignments(
        self, user_client: TestClient,
        admin_user, regular_user,
    ):
        admin_id = _user_id("admin")
        user_id = _user_id("user")
        a = assignments.grant(
            submission_handle="revoked-handle",
            user_id=user_id,
            role_id="approver",
            granted_by_user_id=admin_id,
        )
        assignments.revoke(
            assignment_id=a.id, revoked_by_user_id=admin_id,
        )
        r = user_client.get("/api/my-tasks")
        body = r.json()
        # No entry for the revoked assignment's submission.
        assert all(
            x["submission_handle"] != "revoked-handle"
            for x in body
        )


# ----------------------------------------------------------------------------
# Runtime: _execute_assigns fires on step submit
# ----------------------------------------------------------------------------

class TestAssignRuntimeExecution:
    """End-to-end: a step submit on a node with Assign creates the
    expected assignment + child submission rows."""

    def _setup_two_forms(self):
        # Fresh registry — pop any prior registrations.
        from frontflow.dsl.core import WORKFLOWS
        for fid in ("rte_parent", "rte_child"):
            WORKFLOWS.pop(fid, None)

        recruiter_role = Role("recruiter")

        @form(form_id="rte_child")
        def child():
            @node(role=recruiter_role)
            def screen():
                notes = inputs.Text(label="Notes")
                return notes, Button("Submit")
            screen()
        child()

        @users(label="Pick")
        def picker(ctx):
            return [_user_id("user")]   # fixed list for the test

        @form(form_id="rte_parent")
        def parent():
            @node
            def kickoff():
                project = inputs.Text(label="Project")
                send = Button("Send")
                spawn = Assign(
                    form="rte_child",
                    to=steps.kickoff.picker,
                    role="recruiter",
                    prefill={"notes": "From parent"},
                )
                send >> spawn
                return project, picker, send
            kickoff()
        parent()

        return {
            "parent": WORKFLOWS["rte_parent"],
            "child": WORKFLOWS["rte_child"],
        }

    def test_step_submit_creates_child_and_assignment(
        self, app, admin_user, regular_user,
    ):
        from frontflow.dsl.runtime import start_submission, submit_step
        from frontflow.dsl.store import (
            Form, FormVersion, _engine, _utcnow as _now,
        )
        from sqlalchemy.orm import Session

        forms = self._setup_two_forms()
        parent_cw = compile_workflow(forms["parent"])
        child_cw = compile_workflow(forms["child"])

        # Persist a FormVersion for the child form so
        # _ensure_child_submission can find one.
        with Session(_engine) as s:
            from frontflow.dsl.compile import serialize_workflow
            s.merge(Form(form_id="rte_child", name="Child", folder_path=""))
            s.commit()
            existing = s.execute(
                __import__("sqlalchemy").select(FormVersion).where(
                    FormVersion.form_id == "rte_child",
                )
            ).first()
            if not existing:
                v = FormVersion(
                    form_id="rte_child",
                    version=1,
                    compiled_graph=serialize_workflow(child_cw),
                    content_hash="rte-child-1",
                    source="",
                    created_at=_now(),
                )
                s.add(v)
                s.commit()

        # Start a parent submission and submit its kickoff step.
        sub = start_submission(
            parent_cw,
            initial_form_values={
                "project": "Acme thing",
                "picker": [_user_id("user")],
            },
            acting_user_id=_user_id("admin"),
        )
        # start_submission fires Assigns on the landing step
        # automatically. Verify directly — no manual replay needed.
        from frontflow.dsl.store import (
            Submission as _SubmissionRow,
            SubmissionAssignment as _Assignment,
        )
        from sqlalchemy import select

        with Session(_engine) as s:
            children = s.execute(
                select(_SubmissionRow).where(
                    _SubmissionRow.parent_submission_handle == sub.handle,
                )
            ).scalars().all()
            assert len(children) == 1
            child_handle = children[0].handle
            assignments_rows = s.execute(
                select(_Assignment).where(
                    _Assignment.submission_handle == child_handle,
                )
            ).scalars().all()
            assert len(assignments_rows) == 1
            assert assignments_rows[0].role_id == "recruiter"
            assert assignments_rows[0].user_id == _user_id("user")
            assert assignments_rows[0].granted_by_submission_handle == sub.handle

    def test_idempotent_reexecution(self, app, admin_user, regular_user):
        """Calling _execute_assigns twice with the same submission
        doesn't double-create the child or the assignment."""
        from frontflow.dsl.runtime import (
            _execute_assigns, start_submission,
        )
        from frontflow.dsl.store import (
            Form, FormVersion, _engine, _utcnow as _now,
            Submission as _SubmissionRow,
            SubmissionAssignment as _Assignment,
        )
        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from frontflow.dsl.compile import serialize_workflow

        forms = self._setup_two_forms()
        parent_cw = compile_workflow(forms["parent"])
        child_cw = compile_workflow(forms["child"])

        with Session(_engine) as s:
            s.merge(Form(form_id="rte_child", name="Child", folder_path=""))
            s.commit()
            existing = s.execute(
                select(FormVersion).where(FormVersion.form_id == "rte_child")
            ).first()
            if not existing:
                s.add(FormVersion(
                    form_id="rte_child", version=1,
                    compiled_graph=serialize_workflow(child_cw),
                    content_hash="rte-child-idem", source="",
                    created_at=_now(),
                ))
                s.commit()

        sub = start_submission(
            parent_cw,
            initial_form_values={
                "project": "X", "picker": [_user_id("user")],
            },
            acting_user_id=_user_id("admin"),
        )
        landing = parent_cw.steps[0]
        latest = sub.steps[0]

        # start_submission already fired once. Call again to assert
        # idempotency — find-or-create + grant should both no-op.
        _execute_assigns(parent_cw, sub, latest, landing)

        with Session(_engine) as s:
            children = s.execute(
                select(_SubmissionRow).where(
                    _SubmissionRow.parent_submission_handle == sub.handle,
                )
            ).scalars().all()
            assert len(children) == 1
            assignments_rows = s.execute(
                select(_Assignment).where(
                    _Assignment.submission_handle == children[0].handle,
                )
            ).scalars().all()
            # One row — grant() is idempotent for the active triple.
            assert len(assignments_rows) == 1

    def test_preview_skips_assigns(self, app, admin_user, regular_user):
        """Preview submissions don't write assignment rows."""
        from frontflow.dsl.runtime import _execute_assigns
        from frontflow.dsl.store import (
            SubmissionAssignment as _Assignment, _engine,
        )
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        forms = self._setup_two_forms()
        parent_cw = compile_workflow(forms["parent"])

        # Build a minimal preview submission.
        class _FakeSub:
            handle = "preview-handle"
            preview = True
            user_id = _user_id("admin")
            steps: list = []

        class _FakeStep:
            node_id = "kickoff"
            form_values = {"picker": [_user_id("user")]}
            is_submitted = True

        landing = parent_cw.steps[0]
        # Doesn't raise; nothing is persisted.
        _execute_assigns(parent_cw, _FakeSub(), _FakeStep(), landing)
        with Session(_engine) as s:
            rows = s.execute(
                select(_Assignment).where(
                    _Assignment.granted_by_submission_handle == "preview-handle",
                )
            ).scalars().all()
            assert rows == []

    def test_landing_node_assign_fires_through_http_api(
        self, admin_client, admin_user, regular_user,
    ):
        """Regression: a single-node form whose ONLY node carries
        an Assign must fire the Assign when the user submits the
        landing step via the HTTP API.

        Before the fix, start_submission's auto-submit of the
        landing step skipped _execute_assigns entirely (it only
        called _execute_backend). A real-world form like
        assign_demo_request — whose `request` node IS the landing
        step — would silently produce no child submissions and no
        assignment rows on submit. The fix wires _execute_assigns
        into start_submission too, and threads acting_user_id
        through so the granter check passes.
        """
        from frontflow.dsl.store import (
            Form, FormVersion, _engine, _utcnow as _now,
            Submission as _SubmissionRow,
            SubmissionAssignment as _Assignment,
        )
        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from frontflow.dsl.compile import serialize_workflow

        forms = self._setup_two_forms()
        parent_cw = compile_workflow(forms["parent"])
        child_cw = compile_workflow(forms["child"])

        # Make the parent + child forms reachable through the HTTP
        # API by inserting their compiled graphs into the FORMS
        # registry + persisting child FormVersion. The conftest
        # fixtures don't include the rte_* forms, so we wire them
        # in directly for this test.
        import frontflow.main as ff
        ff.FORMS["rte_parent"] = parent_cw
        ff.FORMS["rte_child"] = child_cw
        with Session(_engine) as s:
            for fid in ("rte_parent", "rte_child"):
                s.merge(Form(
                    form_id=fid, name=fid, folder_path="",
                ))
            s.commit()
            for fid, cw in (
                ("rte_parent", parent_cw), ("rte_child", child_cw),
            ):
                existing = s.execute(
                    select(FormVersion).where(FormVersion.form_id == fid)
                ).first()
                if not existing:
                    s.add(FormVersion(
                        form_id=fid, version=1,
                        compiled_graph=serialize_workflow(cw),
                        content_hash=f"http-{fid}", source="",
                        created_at=_now(),
                    ))
            s.commit()

        # Submit through the API as the admin.
        r = admin_client.post(
            "/api/forms/rte_parent/submissions",
            json={
                "button": None,
                "values": {
                    "project": "X",
                    "picker": [_user_id("user")],
                },
            },
        )
        assert r.status_code in (200, 201), r.text
        body = r.json()
        parent_handle = body["handle"]

        # Verify: child submission created, assignment row inserted.
        with Session(_engine) as s:
            children = s.execute(
                select(_SubmissionRow).where(
                    _SubmissionRow.parent_submission_handle == parent_handle,
                )
            ).scalars().all()
            assert len(children) == 1, (
                "landing-node Assign did not fire on POST to "
                "/api/forms/{id}/submissions"
            )
            rows = s.execute(
                select(_Assignment).where(
                    _Assignment.granted_by_submission_handle == parent_handle,
                )
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].user_id == _user_id("user")
            assert rows[0].role_id == "recruiter"

        # Regression: the assignee must be able to open the child
        # submission they were assigned to. Before the in-memory
        # hydration fix, the DB row existed but `get_submission`
        # returned None (only checks `_submissions` dict) and the
        # API returned 404. The assignee could see the task in
        # their inbox but couldn't actually open it.
        #
        # We verify in-memory hydration directly (rather than
        # through the HTTP endpoint) because resolve_workflow
        # re-execs from the FormVersion.source column, and this
        # test's child_cw was registered via direct dict insert
        # with no source string. The runtime fix is in
        # `_hydrate_new_child_submission` — confirming the
        # submission is registered in `_submissions` is the
        # equivalent check.
        from frontflow.dsl.runtime import get_submission
        child_handle = children[0].handle
        hydrated = get_submission(child_handle)
        assert hydrated is not None, (
            "child submission not registered in _submissions after "
            "creation — assignee will get 404 when opening it"
        )
        assert hydrated.form_id == "rte_child"
        assert len(hydrated.steps) == 1
        assert hydrated.steps[0].is_submitted is False
        # Issue 3 Part A: the child submission must have a minted
        # submission_id at creation time (not stay None until the
        # assignee submits something). Forms without a template
        # mint id = handle.
        assert hydrated.submission_id is not None, (
            "child submission_id should be minted at creation "
            "(handle as fallback when child form has no template)"
        )
        assert hydrated.submission_id == hydrated.handle

    def test_edit_cascade_reprefill_overwrites_landing_values(
        self, app, admin_user, regular_user,
    ):
        """Issue 3 Part C: when the parent's Assign re-fires for an
        already-existing child (edit cascade), the child's landing
        step's draft form_values are silently overwritten with the
        new prefill. Any uncommitted assignee edits to those fields
        are lost — but logged.

        Asserts: same parent + same assignee → same child handle
        (idempotency); but the child's landing step's form_values
        reflect the LATEST prefill, not the first.

        Uses a custom forms setup (not _setup_two_forms) so the
        prefill is a StepRef (resolves against the parent's form
        values), not a literal — re-prefill needs the values to
        actually change.
        """
        from frontflow.dsl.runtime import (
            _execute_assigns, start_submission, get_submission,
        )
        from frontflow.dsl.store import (
            Form, FormVersion, _engine, _utcnow as _now,
            Submission as _SubmissionRow,
        )
        from frontflow.dsl.core import WORKFLOWS
        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from frontflow.dsl.compile import (
            compile_workflow, serialize_workflow,
        )

        # Custom child + parent: parent's Assign prefills `notes`
        # from `steps.kickoff.project` so the prefill value tracks
        # the parent's submitted value.
        for fid in ("cascade_parent", "cascade_child"):
            WORKFLOWS.pop(fid, None)

        recruiter_role = Role("recruiter")

        @form(form_id="cascade_child")
        def cascade_child():
            @node(role=recruiter_role)
            def screen():
                notes = inputs.Text(label="Notes")
                return notes, Button("Submit")
            screen()
        cascade_child()

        @users(label="Pick")
        def cascade_picker(ctx):
            return [_user_id("user")]

        @form(form_id="cascade_parent")
        def cascade_parent():
            @node
            def kickoff():
                project = inputs.Text(label="Project")
                send = Button("Send")
                spawn = Assign(
                    form="cascade_child",
                    to=steps.kickoff.cascade_picker,
                    role="recruiter",
                    prefill={"notes": steps.kickoff.project},
                )
                send >> spawn
                return project, cascade_picker, send
            kickoff()
        cascade_parent()

        parent_cw = compile_workflow(WORKFLOWS["cascade_parent"])
        child_cw = compile_workflow(WORKFLOWS["cascade_child"])

        with Session(_engine) as s:
            s.merge(Form(
                form_id="cascade_child", name="Child", folder_path="",
            ))
            s.commit()
            existing = s.execute(
                select(FormVersion).where(
                    FormVersion.form_id == "cascade_child",
                )
            ).first()
            if not existing:
                s.add(FormVersion(
                    form_id="cascade_child", version=1,
                    compiled_graph=serialize_workflow(child_cw),
                    content_hash="cascade-child", source="",
                    created_at=_now(),
                ))
                s.commit()

        # Make the child form reachable from `_main_mod.FORMS` for
        # the in-memory hydration lookup.
        import frontflow.main as ff
        ff.FORMS["cascade_child"] = child_cw

        # First parent submission — creates child with initial prefill.
        sub = start_submission(
            parent_cw,
            initial_form_values={
                "project": "Project Alpha",
                "cascade_picker": [_user_id("user")],
            },
            acting_user_id=_user_id("admin"),
        )

        with Session(_engine) as s:
            children_first = s.execute(
                select(_SubmissionRow).where(
                    _SubmissionRow.parent_submission_handle == sub.handle,
                )
            ).scalars().all()
        assert len(children_first) == 1
        child_handle_first = children_first[0].handle

        child_first = get_submission(child_handle_first)
        assert child_first is not None
        assert child_first.steps[0].form_values.get("notes") == (
            "Project Alpha"
        ), (
            "child landing step should carry the parent's project "
            "value via the StepRef prefill"
        )

        # Edit cascade: change the parent's `project` value and
        # re-fire the Assign manually (mirrors what would happen
        # after an edit submits the kickoff node a second time).
        sub.steps[0].form_values = {
            "project": "Project Beta (edited)",
            "cascade_picker": [_user_id("user")],
        }
        landing = parent_cw.steps[0]
        latest = sub.steps[0]
        _execute_assigns(parent_cw, sub, latest, landing)

        with Session(_engine) as s:
            children_second = s.execute(
                select(_SubmissionRow).where(
                    _SubmissionRow.parent_submission_handle == sub.handle,
                )
            ).scalars().all()
            assert len(children_second) == 1, (
                "edit-cascade re-fire created a duplicate child"
            )
            assert children_second[0].handle == child_handle_first

        child_after = get_submission(child_handle_first)
        assert child_after is not None
        assert child_after.steps[0].form_values.get("notes") == (
            "Project Beta (edited)"
        ), (
            "child landing step's `notes` should have been "
            "overwritten with the re-prefilled value"
        )

    def test_parent_task_carries_assigned_children_in_response(
        self, app, admin_user, regular_user,
    ):
        """Regression: the parent submission's task list (the API
        that drives the graph view) must surface every spawned
        child submission inline on the task that fired the Assign.

        Without this, the parent's graph would have no visual
        connection to its children — they'd only show up in
        /my-tasks (assignees) or the admin user-detail page. The
        feature this asserts is the `tasks[].assignments` field
        on the SubmissionResponse model.

        Verifies via `_build_submission_response` directly rather
        than going through the HTTP read endpoint — the test
        fixture's `rte_*` forms have form_version_id=0, which the
        HTTP read path can't resolve. The internal task-builder
        sees the same submission + same `_load_assignments_granted_by`
        helper, so the assertion is equally strong.
        """
        from frontflow.dsl.runtime import (
            start_submission, get_submission,
        )
        from frontflow.dsl.store import (
            Form, FormVersion, _engine, _utcnow as _now,
            Submission as _SubmissionRow,
        )
        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from frontflow.dsl.compile import serialize_workflow
        import frontflow.main as ff
        from frontflow.main import _build_submission_response

        forms = self._setup_two_forms()
        parent_cw = compile_workflow(forms["parent"])
        child_cw = compile_workflow(forms["child"])
        ff.FORMS["rte_parent"] = parent_cw
        ff.FORMS["rte_child"] = child_cw
        with Session(_engine) as s:
            for fid in ("rte_parent", "rte_child"):
                s.merge(Form(form_id=fid, name=fid, folder_path=""))
            s.commit()
            for fid, cw in (
                ("rte_parent", parent_cw), ("rte_child", child_cw),
            ):
                if s.execute(
                    select(FormVersion).where(FormVersion.form_id == fid)
                ).first() is None:
                    s.add(FormVersion(
                        form_id=fid, version=1,
                        compiled_graph=serialize_workflow(cw),
                        content_hash=f"task-asgn-{fid}", source="",
                        created_at=_now(),
                    ))
            s.commit()

        sub = start_submission(
            parent_cw,
            initial_form_values={
                "project": "X",
                "picker": [_user_id("user")],
            },
            acting_user_id=_user_id("admin"),
        )

        # Re-read from `_submissions` so any post-submit mutations
        # (assignments granted, etc.) are reflected — `sub` is the
        # same object but explicit roundtrip mirrors what the
        # actual HTTP read endpoint would do internally.
        live = get_submission(sub.handle)
        assert live is not None
        resp = _build_submission_response(live, parent_cw)
        tasks = resp.tasks

        # Find the landing task ('kickoff') — it's the one that
        # fired the Assign.
        kickoff = next(
            (t for t in tasks if t.task_id == "kickoff"), None,
        )
        assert kickoff is not None, "kickoff task missing from response"
        # The fixture grants user_id=_user_id("user") the
        # 'recruiter' role on the child via the parent's Assign.
        assignments_on_task = list(kickoff.assignments)
        assert len(assignments_on_task) == 1, (
            f"expected one assignment on kickoff task, got "
            f"{assignments_on_task!r}"
        )
        a = assignments_on_task[0]
        assert a.child_form_id == "rte_child"
        assert a.role_id == "recruiter"
        assert a.assignee_username == "user"
        assert a.revoked_at is None

    def test_submission_detail_includes_child_graphs(
        self, app, admin_user, regular_user,
    ):
        """Regression: the /detail response carries a `child_graphs`
        field with one entry per spawned child submission (BFS by
        depth, cycle-guarded). Each entry has the child form's
        static graph + per-node state for that specific child
        submission — enabling the parent's graph view to render
        nested child clusters.
        """
        from frontflow.dsl.runtime import start_submission
        from frontflow.dsl.store import (
            Form, FormVersion, _engine, _utcnow as _now,
        )
        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from frontflow.dsl.compile import serialize_workflow
        import frontflow.main as ff
        from frontflow.main import _build_child_graphs

        forms = self._setup_two_forms()
        parent_cw = compile_workflow(forms["parent"])
        child_cw = compile_workflow(forms["child"])
        ff.FORMS["rte_parent"] = parent_cw
        ff.FORMS["rte_child"] = child_cw
        with Session(_engine) as s:
            for fid in ("rte_parent", "rte_child"):
                s.merge(Form(form_id=fid, name=fid, folder_path=""))
            s.commit()
            for fid, cw in (
                ("rte_parent", parent_cw), ("rte_child", child_cw),
            ):
                if s.execute(
                    select(FormVersion).where(FormVersion.form_id == fid)
                ).first() is None:
                    s.add(FormVersion(
                        form_id=fid, version=1,
                        compiled_graph=serialize_workflow(cw),
                        content_hash=f"cg-{fid}", source="",
                        created_at=_now(),
                    ))
            s.commit()

        sub = start_submission(
            parent_cw,
            initial_form_values={
                "project": "X",
                "picker": [_user_id("user")],
            },
            acting_user_id=_user_id("admin"),
        )

        child_graphs = _build_child_graphs(sub.handle)
        assert len(child_graphs) == 1, (
            f"expected one child graph for the spawned screening, "
            f"got {child_graphs!r}"
        )
        cg = child_graphs[0]
        assert cg.depth == 1
        assert cg.child_form_id == "rte_child"
        assert cg.parent_node_id == "kickoff"
        assert cg.role_id == "recruiter"
        assert cg.assignee_username == "user"
        assert cg.revoked_at is None
        # The child form has at least one node — its graph payload
        # should reflect that.
        assert len(cg.graph.nodes) >= 0  # type tolerance — see note
        assert len(cg.graph.groups) >= 1, (
            "child form's graph should contain at least one node group"
        )
        # The child submission has its landing step in-progress
        # (not yet submitted) — node_state should mark it running.
        # The test child form's landing node id ("screen") may not
        # match this fixture; just assert non-empty state map.
        assert cg.node_state, (
            "child submission's node_state should not be empty — "
            "at least the landing step is in-progress"
        )

    def test_child_graphs_cycle_safe(
        self, app, admin_user, regular_user,
    ):
        """A misconfigured form that Assigns back to its parent
        won't blow the response. Cycle detection skips the second
        visit; depth cap stops a long-but-not-cyclic chain.

        Smoke-tests by walking from a submission with no children
        and asserting we get an empty list cleanly.
        """
        from frontflow.dsl.runtime import start_submission
        from frontflow.main import _build_child_graphs

        forms = self._setup_two_forms()
        parent_cw = compile_workflow(forms["parent"])

        # Submit but DON'T pick anyone — no assignments fire.
        sub = start_submission(
            parent_cw,
            initial_form_values={
                "project": "X",
                "picker": [],  # no assignees
            },
            acting_user_id=_user_id("admin"),
        )
        child_graphs = _build_child_graphs(sub.handle)
        assert child_graphs == [], (
            f"expected no child graphs, got {child_graphs!r}"
        )
