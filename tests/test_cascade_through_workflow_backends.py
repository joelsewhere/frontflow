"""Regression for the stale-data bug Connor reported: when the
user edits a HITL step that feeds workflow-level @backend steps,
downstream backends that read transient outputs (e.g. files from
the first backend) won't re-run if they DON'T declare a dependency
on the first backend.

The cascade walks each step's declared `steps.*` args. If a chart
backend reads a CSV produced by `transform` but doesn't declare
`steps.transform` as an arg, the cascade doesn't know the chart
depends on transform — when the user edits the HITL step that
transform reads from, only transform re-runs; the chart keeps its
stale return.

Fix is form-side: declare `steps.<producing_step>` as an arg on
every downstream backend that reads its output, even if the value
isn't used directly in the function body. The arg is the
dependency declaration.
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


def _build_form(*, kpi_declares_transform_dep: bool, form_id: str):
    """Two workflow-level backends in series: `transform` produces
    a deterministic key (same every run), `compute_kpi` reads
    something keyed by it.

    The toggle `kpi_declares_transform_dep` controls whether
    `compute_kpi` declares `steps.transform` as an arg. With it
    set False, the cascade doesn't see the dependency and
    compute_kpi keeps its stale return after editing distribute.
    """

    # Shared in-process counter so the test can see whether
    # compute_kpi actually re-ran. Reset between cases by the
    # caller via `_call_counts`.
    @form(form_id=form_id, title="x")
    def f():

        @node
        def landing():
            name = inputs.Text(id="name", label="Name", required=True)
            return name, Button("Next")

        @node
        def distribute():
            policy = inputs.Text(id="policy", label="policy")
            submit = Button("Apply")
            return policy, submit

        @backend(hidden=True)
        def transform(mapping):
            """Returns the policy string verbatim — a clean way to
            check that it actually re-ran on a different policy."""
            _call_counts["transform"] += 1
            return mapping or "no_mapping"

        if kpi_declares_transform_dep:
            @backend(hidden=True)
            def compute_kpi(transform_out):
                _call_counts["compute_kpi"] += 1
                return {"value": transform_out}
        else:
            @backend(hidden=True)
            def compute_kpi():
                _call_counts["compute_kpi"] += 1
                return {"value": "STALE"}

        @node
        def review():
            return (
                displays.Markdown("done"),
                Button("OK"),
            )

        ln = landing()
        dist = distribute()
        rv = review()

        tr = transform(steps.distribute["policy"])
        if kpi_declares_transform_dep:
            kp = compute_kpi(steps.transform)
        else:
            kp = compute_kpi()

        ln >> dist >> tr >> kp >> rv

    f()
    return compile_workflow(WORKFLOWS[form_id])


_call_counts: dict[str, int] = {"transform": 0, "compute_kpi": 0}


def _reset_counts():
    _call_counts["transform"] = 0
    _call_counts["compute_kpi"] = 0


def test_cascade_skips_compute_kpi_without_declared_dep():
    """The buggy case: compute_kpi declares no dep on transform.
    Editing distribute re-runs transform but leaves compute_kpi's
    stale return in place. Documents the failure mode that
    motivated the form-side declared-dep workaround."""
    _reset_counts()
    cw = _build_form(
        kpi_declares_transform_dep=False, form_id="cascade_buggy",
    )
    sub = runtime.start_submission(cw, {"name": "x"})
    runtime.advance(cw, sub)
    runtime.submit_step(cw, sub, "distribute",
                        {"policy": "first"}, button_clicked=None)
    runtime.advance(cw, sub)
    first_kpi_calls = _call_counts["compute_kpi"]
    assert first_kpi_calls == 1

    # Now EDIT distribute and resubmit with a different policy.
    runtime.clear_submission_from(
        cw, sub, "distribute", mode="edit", scope="cascade",
    )
    runtime.submit_step(cw, sub, "distribute",
                        {"policy": "second"}, button_clicked=None)
    runtime.advance(cw, sub)

    # transform re-ran (its arg changed).
    assert _call_counts["transform"] == 2
    # But compute_kpi DID NOT — no declared dep on transform.
    assert _call_counts["compute_kpi"] == first_kpi_calls, (
        f"expected compute_kpi to stay stale; ran "
        f"{_call_counts['compute_kpi']} times total"
    )


def test_cascade_reruns_compute_kpi_with_declared_dep():
    """The fix: declaring `steps.transform` as an arg gives the
    cascade a dep edge. Editing distribute now re-runs both
    transform AND compute_kpi."""
    _reset_counts()
    cw = _build_form(
        kpi_declares_transform_dep=True, form_id="cascade_fixed",
    )
    sub = runtime.start_submission(cw, {"name": "x"})
    runtime.advance(cw, sub)
    runtime.submit_step(cw, sub, "distribute",
                        {"policy": "first"}, button_clicked=None)
    runtime.advance(cw, sub)
    assert _call_counts["compute_kpi"] == 1

    runtime.clear_submission_from(
        cw, sub, "distribute", mode="edit", scope="cascade",
    )
    runtime.submit_step(cw, sub, "distribute",
                        {"policy": "second"}, button_clicked=None)
    runtime.advance(cw, sub)

    # Both re-ran.
    assert _call_counts["transform"] == 2
    assert _call_counts["compute_kpi"] == 2, (
        f"compute_kpi should have re-run via the declared "
        f"`steps.transform` dep; ran {_call_counts['compute_kpi']} "
        f"times total"
    )

    # The submission terminated cleanly, downstream values reflect
    # the new policy.
    kpi_step = next(s for s in sub.steps if s.node_id == "compute_kpi")
    assert kpi_step.backend_return == {"value": "second"}
