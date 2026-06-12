"""Regression for the step.error persistence + surfacing path.

A failing backend records its exception on the step (`step.error`).
That message needs to:
  - persist through `submission_snapshot` → DB → `load_submissions`
    so it survives a restart
  - surface in both API models that drive the UI: `StepDetailRow`
    (submission-detail page) and `TaskInstance` (chain UI)

Before this change, errors lived only in memory and the chain UI's
backend-step component had no `error` prop at all. Users saw the
bare "Backend step · failed" label with no way to know why.
"""
from __future__ import annotations

import pytest

from frontflow import Button, backend, displays, form, inputs, node, steps
from frontflow.dsl.compile import compile_workflow
from frontflow.dsl.core import WORKFLOWS
from frontflow.dsl import runtime


@pytest.fixture(autouse=True)
def _isolate():
    yield
    WORKFLOWS.clear()


def _failing_workflow(form_id: str):
    @form(form_id=form_id, title="x")
    def f():

        @node
        def landing():
            name = inputs.Text(id="name", label="Name", required=True)
            return name, Button("Next")

        @node
        def setup():
            x = inputs.Text(id="x", label="x")
            return x, Button("Go")

        @backend(hidden=True)
        def faulty(x):
            # Reproduces the int(NaN) shape Connor was hitting
            # without needing pandas.
            raise ValueError("cannot convert float NaN to integer")

        @node
        def done():
            return (displays.Markdown("done"),)

        ln = landing()
        s = setup()
        dn = done()
        fl = faulty(steps.setup["x"])

        ln >> s >> fl >> dn

    f()
    return compile_workflow(WORKFLOWS[form_id])


class TestStepErrorPersistence:
    """`step.error` survives `submission_snapshot` → `hydrate_submission`
    round-trip, just like any other persisted step field."""

    def test_error_in_snapshot(self):
        cw = _failing_workflow("err_snapshot")
        sub = runtime.start_submission(cw, {"name": "Connor"})
        runtime.advance(cw, sub)
        runtime.submit_step(cw, sub, "setup",
                            {"x": "ok"}, button_clicked=None)
        runtime.advance(cw, sub)
        assert sub.failed

        snap = runtime.submission_snapshot(cw, sub)
        faulty_step = next(
            s for s in snap["steps"] if s["node_id"] == "faulty"
        )
        # Backend's raised exception is stored verbatim, with the
        # exception class prefix from `_run_backend_step`.
        assert faulty_step["error"] == (
            "ValueError: cannot convert float NaN to integer"
        )
        assert faulty_step["state"] == "failed"
        # Full Python traceback captured at the raise site —
        # multi-line, starts with the conventional header, ends
        # with the exception line.
        tb = faulty_step["traceback"]
        assert tb is not None
        assert "Traceback (most recent call last)" in tb
        assert "ValueError: cannot convert float NaN to integer" in tb

    def test_error_roundtrips_through_hydration(self):
        cw = _failing_workflow("err_hydrate")
        sub = runtime.start_submission(cw, {"name": "Connor"})
        runtime.advance(cw, sub)
        runtime.submit_step(cw, sub, "setup",
                            {"x": "ok"}, button_clicked=None)
        runtime.advance(cw, sub)

        snap = runtime.submission_snapshot(cw, sub)
        # Clear in-memory state and rehydrate. This simulates a
        # frontflow restart loading the persisted snapshot.
        runtime._submissions.clear()
        rehydrated = runtime.hydrate_submission(snap, "err_hydrate")

        faulty_step = next(
            s for s in rehydrated.steps if s.node_id == "faulty"
        )
        assert faulty_step.error == (
            "ValueError: cannot convert float NaN to integer"
        ), (
            f"expected error to round-trip; got {faulty_step.error!r}"
        )
        # Traceback survives the round-trip too — needed so a
        # restart doesn't drop the diagnostic info Connor uses to
        # debug a stuck submission.
        assert faulty_step.traceback is not None
        assert "Traceback" in faulty_step.traceback
        # Submission-level failed flag too.
        assert rehydrated.failed


def _arg_resolution_failure_workflow(form_id: str):
    """A workflow where the BACKEND ITSELF doesn't raise — its
    argument resolution does. The function reads a dict field, but
    the step ref the form wires up returns None.

    Reproduces what Connor hit on a real upload: `transform`'s arg
    chain pulled a None from a previous backend's return shape (or
    from a non-dict field-ref), and the resulting AttributeError /
    TypeError happened BEFORE entering the function body. The runtime
    used to let that bubble up uncaught."""
    @form(form_id=form_id, title="x")
    def f():

        @node
        def landing():
            name = inputs.Text(id="name", label="Name", required=True)
            return name, Button("Next")

        @backend(hidden=True)
        def upstream():
            # Returns a non-dict scalar. The downstream backend
            # wires `steps.upstream["some_field"]` — a TypeError
            # against `None["some_field"]` (since field-ref on a
            # non-dict resolves to None per the runtime), then
            # downstream calls `.upper()` on it.
            return None

        @backend(hidden=True)
        def downstream(value):
            # `value` is None — calling .upper() raises AttributeError.
            return value.upper()

        @node
        def done():
            return (displays.Markdown("done"),)

        ln = landing()
        dn = done()
        up = upstream()
        ds = downstream(steps.upstream["some_field"])

        ln >> up >> ds >> dn

    f()
    return compile_workflow(WORKFLOWS[form_id])


class TestArgResolutionErrors:
    """Even an exception during argument resolution (not in the
    function body) must be captured on the step and surfaced. The
    transform-with-no-error-anywhere bug came from this exact gap
    — `_resolve_step_ref` raising outside the try caused the
    submission to hang in a non-advancing state with `step.error`
    never set."""

    def test_downstream_failure_captured_as_step_error(self):
        cw = _arg_resolution_failure_workflow("arg_resolve_fail")
        sub = runtime.start_submission(cw, {"name": "Connor"})
        runtime.advance(cw, sub)
        assert sub.failed, (
            "submission should be marked failed when a backend's "
            "argument resolution / invocation raises"
        )
        downstream_step = next(
            s for s in sub.steps if s.node_id == "downstream"
        )
        assert downstream_step.error is not None, (
            "step.error must be set so the chain UI can render it"
        )
        # AttributeError from `.upper()` on None — exact class
        # matters less than the fact that SOMETHING is captured.
        assert "AttributeError" in downstream_step.error
