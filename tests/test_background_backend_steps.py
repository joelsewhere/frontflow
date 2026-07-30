"""Background execution of workflow-level @backend steps.

`@form(background_backends=True)` makes `advance()` hand the step
loop to a worker thread: the calling request returns immediately with
the frontier backend step still unsubmitted (surfaced as "running" by
the task assembly), and the worker completes the steps while the UI
polls. Default forms keep the synchronous contract.
"""
from __future__ import annotations

import threading
import time

import pytest

from frontflow import Button, backend, displays, form, inputs, node, steps
from frontflow.dsl.compile import compile_workflow
from frontflow.dsl.core import WORKFLOWS
from frontflow.dsl import runtime


@pytest.fixture(autouse=True)
def _isolate():
    yield
    WORKFLOWS.clear()


def _build(form_id: str, *, background: bool, gate: threading.Event):
    """landing (HITL) -> slow (workflow @backend, blocks on `gate`)
    -> done (terminal node)."""

    @form(form_id=form_id, background_backends=background)
    def f():

        @node
        def landing():
            name = inputs.Text(id="name", label="Name", required=True)
            return name, Button("Next")

        @backend(hidden=False)
        def slow(name_value):
            gate.wait(timeout=5)
            return f"done:{name_value}"

        @node
        def done():
            return (displays.Markdown("finished"),)

        landing_node = landing()
        # Bracket access — `.name` collides with StepRef's own
        # attribute (same reason forms write steps.<node>["name"]).
        slow_step = slow(steps.landing["name"])
        done_node = done()
        landing_node >> slow_step >> done_node

    f()
    return compile_workflow(WORKFLOWS[form_id])


def _slow_step(submission):
    return next(s for s in submission.steps if s.node_id == "slow")


def test_background_advance_returns_before_step_completes(monkeypatch):
    # The worker persists on completion; don't hit a real DB from the
    # test — record the attempt instead.
    persisted = []
    monkeypatch.setattr(
        runtime.store, "sync_submission", lambda snap: persisted.append(snap),
    )
    gate = threading.Event()
    wf = _build("bg_on", background=True, gate=gate)
    sub = runtime.start_submission(wf, {"name": "x"})
    # start_submission stops at the submitted landing; this advance —
    # what the first poll does — routes to the backend step, hands the
    # loop to the worker, and returns immediately.
    runtime.advance(wf, sub)

    # The advance must have come back with the backend step routed but
    # NOT finished — it's running in the worker.
    step = _slow_step(sub)
    assert not step.is_submitted

    # Same-submission advance calls while the worker owns it are
    # no-ops (this is what every poll does).
    runtime.advance(wf, sub)
    assert not step.is_submitted

    # Release the worker; the step completes and routing continues to
    # the terminal node without any further request.
    gate.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not sub.terminated:
        time.sleep(0.02)
    assert _slow_step(sub).is_submitted
    assert _slow_step(sub).backend_return == "done:x"
    assert sub.terminated
    # The worker persisted the completed state (submission_id exists,
    # not preview) and released the in-flight flag.
    assert persisted
    assert sub.handle not in runtime._background_advances


def test_default_form_stays_synchronous():
    gate = threading.Event()
    gate.set()  # never block — sync path would deadlock the test
    wf = _build("bg_off", background=False, gate=gate)
    sub = runtime.start_submission(wf, {"name": "y"})
    runtime.advance(wf, sub)

    # Synchronous contract: by the time submit returns, the backend
    # step ran and the submission reached the terminal node.
    assert _slow_step(sub).is_submitted
    assert sub.terminated
