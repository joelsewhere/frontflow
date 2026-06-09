"""Phase 5 tests — lifecycle hooks + signed links.

Covers:
  - signed_links.mint produces a valid token
  - signed_links.verify accepts a fresh token
  - signed_links.verify rejects tampered, expired, mismatched-handle,
    wrong-issuer tokens
  - on_submitted fires when a submission reaches terminal success
  - on_failed fires when a submission terminates failed
  - on_revoked fires when an assignment is revoked
  - Hook failures are swallowed (logged, not raised)
  - signed_link_token is present on the on_assigned event
"""
from __future__ import annotations

import os
import time

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from frontflow import (
    Button, Role, form, inputs, node, users,
)
from frontflow.dsl import assignments, signed_links
from frontflow.dsl.core import WORKFLOWS
from frontflow.dsl.store import User, _engine


def _user_id(username: str) -> int:
    with Session(_engine) as s:
        return s.execute(
            select(User).where(User.username == username)
        ).scalar_one().id


# ----------------------------------------------------------------------------
# signed_links — mint + verify
# ----------------------------------------------------------------------------

class TestSignedLinks:
    def test_mint_then_verify_roundtrip(self):
        token = signed_links.mint(
            user_id=42,
            submission_handle="sub-abc",
            scope="fill",
            issuer="assign_operator",
            ttl_seconds=60,
        )
        payload = signed_links.verify(
            token, submission_handle="sub-abc",
        )
        assert payload is not None
        assert payload["user_id"] == 42
        assert payload["submission_handle"] == "sub-abc"
        assert payload["scope"] == "fill"
        assert payload["issuer"] == "assign_operator"

    def test_verify_rejects_mismatched_handle(self):
        token = signed_links.mint(
            user_id=42, submission_handle="sub-abc",
        )
        assert signed_links.verify(
            token, submission_handle="sub-other",
        ) is None

    def test_verify_rejects_expired(self):
        token = signed_links.mint(
            user_id=42, submission_handle="sub-abc", ttl_seconds=1,
        )
        time.sleep(2)
        assert signed_links.verify(
            token, submission_handle="sub-abc",
        ) is None

    def test_verify_rejects_tampered_payload(self):
        token = signed_links.mint(
            user_id=42, submission_handle="sub-abc",
        )
        # Flip a character in the payload section.
        payload, sig = token.split(".")
        # Cheap mutation: append a char.
        tampered = (payload + "x") + "." + sig
        assert signed_links.verify(
            tampered, submission_handle="sub-abc",
        ) is None

    def test_verify_rejects_bad_signature(self):
        token = signed_links.mint(
            user_id=42, submission_handle="sub-abc",
        )
        payload, sig = token.split(".")
        # Flip a character in the signature.
        bad_sig = "A" + sig[1:] if sig[0] != "A" else "B" + sig[1:]
        tampered = payload + "." + bad_sig
        assert signed_links.verify(
            tampered, submission_handle="sub-abc",
        ) is None

    def test_verify_rejects_malformed(self):
        assert signed_links.verify(
            "not-a-token", submission_handle="x",
        ) is None
        assert signed_links.verify("", submission_handle="x") is None
        assert signed_links.verify(
            "only.one.dot.too.many", submission_handle="x",
        ) is None

    def test_verify_require_issuer(self):
        token = signed_links.mint(
            user_id=42, submission_handle="sub-abc",
            issuer="assign_operator",
        )
        # Wrong required issuer → None.
        assert signed_links.verify(
            token, submission_handle="sub-abc", require_issuer="embed",
        ) is None
        # Matching → payload.
        assert signed_links.verify(
            token, submission_handle="sub-abc",
            require_issuer="assign_operator",
        ) is not None

    def test_mint_rejects_bad_scope(self):
        with pytest.raises(ValueError, match="scope"):
            signed_links.mint(
                user_id=1, submission_handle="x", scope="manage",
            )

    def test_mint_rejects_bad_issuer(self):
        with pytest.raises(ValueError, match="issuer"):
            signed_links.mint(
                user_id=1, submission_handle="x", issuer="random",
            )

    def test_mint_caps_ttl(self):
        # Pass a huge TTL; the cap silently reduces it.
        token = signed_links.mint(
            user_id=1, submission_handle="x",
            ttl_seconds=10**9,  # ~30 years
        )
        payload = signed_links.verify(token, submission_handle="x")
        assert payload is not None
        # exp - iat should be capped at 90 days.
        assert payload["exp"] - payload["iat"] <= 90 * 24 * 3600

    def test_build_link(self):
        url = signed_links.build_link(
            base_url="https://forms.example.com/",
            form_id="my_form",
            submission_handle="sub-abc",
            token="TOK",
        )
        assert url == (
            "https://forms.example.com/forms/my_form/form/"
            "submission/sub-abc?token=TOK"
        )


# ----------------------------------------------------------------------------
# Lifecycle hooks — on_submitted, on_failed, on_revoked
# ----------------------------------------------------------------------------


class TestLifecycleHooks:
    def test_on_submitted_fires_on_terminal_success(self, app):
        captured: list = []

        def on_sub(event):
            captured.append(event)

        WORKFLOWS.pop("hook_sub_form", None)

        @form(form_id="hook_sub_form", on_submitted=on_sub)
        def wf():
            @node
            def step1():
                x = inputs.Text(label="X")
                return x, Button("Submit")
            step1()
        wf()

        from frontflow.dsl.runtime import advance, start_submission
        from frontflow.dsl.compile import compile_workflow
        cw = compile_workflow(WORKFLOWS["hook_sub_form"])

        sub = start_submission(
            cw, initial_form_values={"x": "value"},
        )
        advance(cw, sub)
        # Single-step form auto-terminates after landing submit.
        assert sub.terminated is True
        assert len(captured) == 1
        assert captured[0]["kind"] == "submitted"
        assert captured[0]["form_id"] == "hook_sub_form"
        assert captured[0]["submission_handle"] == sub.handle

    def test_on_submitted_fires_only_once(self, app):
        captured: list = []

        def on_sub(event):
            captured.append(event)

        WORKFLOWS.pop("hook_once_form", None)

        @form(form_id="hook_once_form", on_submitted=on_sub)
        def wf():
            @node
            def step1():
                x = inputs.Text(label="X")
                return x, Button("Submit")
            step1()
        wf()

        from frontflow.dsl.runtime import advance, start_submission
        from frontflow.dsl.compile import compile_workflow
        cw = compile_workflow(WORKFLOWS["hook_once_form"])

        sub = start_submission(cw, initial_form_values={"x": "v"})
        advance(cw, sub)
        # Calling advance again should NOT re-fire.
        advance(cw, sub)
        advance(cw, sub)
        assert len(captured) == 1

    def test_on_submitted_hook_failure_doesnt_crash(self, app):
        def boom(event):
            raise RuntimeError("intentional")

        WORKFLOWS.pop("hook_boom_form", None)

        @form(form_id="hook_boom_form", on_submitted=boom)
        def wf():
            @node
            def step1():
                x = inputs.Text(label="X")
                return x, Button("Submit")
            step1()
        wf()

        from frontflow.dsl.runtime import advance, start_submission
        from frontflow.dsl.compile import compile_workflow
        cw = compile_workflow(WORKFLOWS["hook_boom_form"])

        sub = start_submission(cw, initial_form_values={"x": "v"})
        # Should not raise.
        advance(cw, sub)
        assert sub.terminated is True

    def test_on_revoked_fires_on_revoke(
        self, app, admin_user, regular_user,
    ):
        captured: list = []

        def on_rev(event):
            captured.append(event)

        # Use an EXISTING fixture form (auth_gated_form) and
        # register a hook on it. The WORKFLOWS dict's instance
        # is the one runtime hooks reach for.
        wf = WORKFLOWS["auth_gated_form"]
        wf.on_revoked = on_rev

        # Need an actual Submission row that points to a
        # FormVersion for the form, since on_revoked resolves
        # form_id via submission → form_version → form.
        from frontflow.dsl.store import (
            Form, FormVersion, Submission as _SubRow, _utcnow as _now,
        )
        from frontflow.dsl.compile import (
            compile_workflow, serialize_workflow,
        )
        cw = compile_workflow(wf)

        with Session(_engine) as s:
            s.merge(Form(
                form_id="auth_gated_form",
                name="Auth gated form",
                folder_path="",
            ))
            s.commit()
            ver = s.execute(
                select(FormVersion).where(
                    FormVersion.form_id == "auth_gated_form",
                )
            ).scalar_one_or_none()
            if ver is None:
                ver = FormVersion(
                    form_id="auth_gated_form",
                    version=1,
                    compiled_graph=serialize_workflow(cw),
                    content_hash="rev-1",
                    source="",
                    created_at=_now(),
                )
                s.add(ver)
                s.commit()
            s.add(_SubRow(
                handle="rev-test-handle",
                submission_id="rev-test-id",
                form_version_id=ver.id,
                state="running",
                created_at=_now(),
                updated_at=_now(),
            ))
            s.commit()

        # Grant + revoke.
        admin_id = _user_id("admin")
        user_id = _user_id("user")
        a = assignments.grant(
            submission_handle="rev-test-handle",
            user_id=user_id,
            role_id="approver",
            granted_by_user_id=admin_id,
        )
        assignments.revoke(
            assignment_id=a.id, revoked_by_user_id=admin_id,
        )

        assert len(captured) == 1
        event = captured[0]
        assert event["kind"] == "revoked"
        assert event["form_id"] == "auth_gated_form"
        assert event["submission_handle"] == "rev-test-handle"
        assert event["assignee_user_id"] == user_id
        assert event["role_id"] == "approver"
        assert event["revoked_by_user_id"] == admin_id

        # Cleanup: restore the form's on_revoked so other tests
        # in this module don't capture stray events.
        wf.on_revoked = None


# ----------------------------------------------------------------------------
# signed_link_token appears on on_assigned event
# ----------------------------------------------------------------------------


class TestOnAssignedSignedLink:
    """The on_assigned hook should receive a usable signed-link
    token alongside the existing event fields."""

    def test_event_carries_signed_link_token(
        self, app, admin_user, regular_user,
    ):
        from frontflow.dsl.runtime import _fire_on_assigned_hook

        captured: list = []

        def hook(event):
            captured.append(event)

        class _FakeSub:
            handle = "fake-parent-handle"

        _fire_on_assigned_hook(
            hook,
            parent_form_id="parent",
            parent_submission=_FakeSub(),
            child_submission_handle="child-handle-1",
            child_form_id="child",
            assignee_user_id=_user_id("user"),
            role_id="recruiter",
            assignment_row_id=1,
            link_ttl_days=7,
        )
        assert len(captured) == 1
        evt = captured[0]
        assert "signed_link_token" in evt
        token = evt["signed_link_token"]
        assert token is not None
        # Roundtrip-verify the token.
        payload = signed_links.verify(
            token, submission_handle="child-handle-1",
        )
        assert payload is not None
        assert payload["user_id"] == _user_id("user")
        assert payload["scope"] == "fill"
        assert payload["issuer"] == "assign_operator"


# ----------------------------------------------------------------------------
# on_failed — terminal-failure hook coverage (Likely-next-ask #7)
# ----------------------------------------------------------------------------

class TestOnFailedHook:
    """`on_failed` fires when a submission terminates with failed=True.

    The HANDOFF flagged this hook as reachable but never tested. A
    submission goes failed when a `@backend` step raises — the
    runtime sets `submission.failed = True` and records a
    `submission_failed` event. `_fire_terminal_hook(kind='failed')`
    then invokes the form's `on_failed` callable with an event dict
    carrying `{kind, form_id, submission_handle, submission_id,
    user_id, error}`.

    These tests drive that path through `advance()`, so they cover
    the full chain — not just a synthetic call to the hook firer.
    """

    def test_on_failed_fires_on_backend_raise(self, app):
        from frontflow import backend  # local import — registered via DSL star
        captured: list = []

        def on_fail(event):
            captured.append(event)

        WORKFLOWS.pop("hook_fail_form", None)

        @form(form_id="hook_fail_form", on_failed=on_fail)
        def wf():
            @node
            def step1():
                x = inputs.Text(label="X")
                submit = Button("Submit")

                @backend
                def boom(x):
                    raise RuntimeError("intentional-failure")

                submit >> boom(x)
                return x, submit
            step1()
        wf()

        from frontflow.dsl.runtime import advance, start_submission
        from frontflow.dsl.compile import compile_workflow
        cw = compile_workflow(WORKFLOWS["hook_fail_form"])

        sub = start_submission(
            cw, initial_form_values={"x": "anything"},
        )
        advance(cw, sub)

        # The backend raised → submission terminated with failed=True.
        assert sub.failed is True, (
            f"submission did not enter failed state; sub.failed={sub.failed}"
        )
        # And the hook fired exactly once with the right event shape.
        assert len(captured) == 1, (
            f"on_failed should fire once on terminal failure; "
            f"fired {len(captured)} times"
        )
        evt = captured[0]
        assert evt["kind"] == "failed"
        assert evt["form_id"] == "hook_fail_form"
        assert evt["submission_handle"] == sub.handle
        # The error string should reference the failing backend call.
        assert evt["error"] is not None
        assert "intentional-failure" in evt["error"]

    def test_on_failed_does_not_fire_on_terminal_success(self, app):
        """A submission that terminates cleanly must hit on_submitted,
        NOT on_failed. Guards against a hook dispatcher that fires
        both, or fires on_failed on any terminal state."""
        failed_captured: list = []
        sub_captured: list = []

        WORKFLOWS.pop("hook_success_only_form", None)

        @form(
            form_id="hook_success_only_form",
            on_submitted=lambda e: sub_captured.append(e),
            on_failed=lambda e: failed_captured.append(e),
        )
        def wf():
            @node
            def step1():
                x = inputs.Text(label="X")
                return x, Button("Submit")
            step1()
        wf()

        from frontflow.dsl.runtime import advance, start_submission
        from frontflow.dsl.compile import compile_workflow
        cw = compile_workflow(WORKFLOWS["hook_success_only_form"])

        sub = start_submission(cw, initial_form_values={"x": "ok"})
        advance(cw, sub)

        assert sub.failed is False
        assert sub.terminated is True
        assert len(sub_captured) == 1, (
            "on_submitted should fire on terminal success"
        )
        assert len(failed_captured) == 0, (
            f"on_failed should NOT fire on terminal success; "
            f"got {failed_captured}"
        )

    def test_on_failed_fires_only_once(self, app):
        """A re-call to advance() after a failed termination must
        not re-fire the hook. The `_terminal_hook_fired` flag is
        the safety net; this test exercises it for the failed
        branch."""
        from frontflow import backend
        captured: list = []

        WORKFLOWS.pop("hook_fail_once_form", None)

        @form(
            form_id="hook_fail_once_form",
            on_failed=lambda e: captured.append(e),
        )
        def wf():
            @node
            def step1():
                x = inputs.Text(label="X")
                submit = Button("Submit")

                @backend
                def boom(x):
                    raise RuntimeError("once")

                submit >> boom(x)
                return x, submit
            step1()
        wf()

        from frontflow.dsl.runtime import advance, start_submission
        from frontflow.dsl.compile import compile_workflow
        cw = compile_workflow(WORKFLOWS["hook_fail_once_form"])

        sub = start_submission(cw, initial_form_values={"x": "v"})
        advance(cw, sub)
        # Two more advance calls — should NOT re-fire.
        advance(cw, sub)
        advance(cw, sub)
        assert len(captured) == 1

    def test_on_failed_hook_failure_doesnt_crash(self, app):
        """Same swallow-and-log contract as on_submitted — a
        raising hook must not bubble into advance(). Verifies the
        failed-branch path through `_fire_terminal_hook`."""
        from frontflow import backend

        def boom_hook(event):
            raise RuntimeError("hook-internal-fail")

        WORKFLOWS.pop("hook_fail_boom_form", None)

        @form(
            form_id="hook_fail_boom_form",
            on_failed=boom_hook,
        )
        def wf():
            @node
            def step1():
                x = inputs.Text(label="X")
                submit = Button("Submit")

                @backend
                def boom(x):
                    raise RuntimeError("step-internal-fail")

                submit >> boom(x)
                return x, submit
            step1()
        wf()

        from frontflow.dsl.runtime import advance, start_submission
        from frontflow.dsl.compile import compile_workflow
        cw = compile_workflow(WORKFLOWS["hook_fail_boom_form"])

        sub = start_submission(cw, initial_form_values={"x": "v"})
        # Two layers of exception — hook + backend — both must be
        # contained. advance() should return normally and the
        # submission should still be in its failed terminal state.
        advance(cw, sub)
        assert sub.failed is True

    def test_on_failed_optional_when_unset(self, app):
        """A form with no on_failed hook should still reach terminal
        failure without error. Implicit no-op behavior."""
        from frontflow import backend

        WORKFLOWS.pop("hook_fail_none_form", None)

        @form(form_id="hook_fail_none_form")  # no on_failed
        def wf():
            @node
            def step1():
                x = inputs.Text(label="X")
                submit = Button("Submit")

                @backend
                def boom(x):
                    raise RuntimeError("e")

                submit >> boom(x)
                return x, submit
            step1()
        wf()

        from frontflow.dsl.runtime import advance, start_submission
        from frontflow.dsl.compile import compile_workflow
        cw = compile_workflow(WORKFLOWS["hook_fail_none_form"])

        sub = start_submission(cw, initial_form_values={"x": "v"})
        # Should reach failed state without raising.
        advance(cw, sub)
        assert sub.failed is True
