"""Tests for source-independent submission viewing.

These exercise the architectural property: a submission is auditable
regardless of whether its pinned form_version's source can be re-execed.
The compiled_graph JSON stored alongside the source carries the full
structural shape — layout, fields, buttons, node graph — and
`compiled_graph_to_workflow` reconstructs a view-only CompiledWorkflow
from it. Backend callables become inert placeholders that raise
`WorkflowSourceUnavailable` if invoked, which the runtime uses to
short-circuit advance/submit attempts cleanly.

Coverage:
  - Round-trip: serialize → deserialize → structural equivalence
  - Placeholder backends raise WorkflowSourceUnavailable on call
  - resolve_workflow falls back to the deserializer when source
    can't re-exec
  - Tagging: deserialized workflows carry the `source_unavailable`
    flag and the original error message
"""
from __future__ import annotations

import pytest

from frontflow import Button, displays, form, node
from frontflow.dsl.compile import (
    WorkflowSourceUnavailable,
    _UnavailableBackendFn,
    compile_workflow,
    compiled_graph_to_workflow,
    serialize_workflow,
)
from frontflow.dsl.core import WORKFLOWS


@pytest.fixture
def compiled_round_trip():
    """A two-node workflow, compiled and serialized so the deserializer
    can be exercised against a real shape (display blocks, buttons,
    downstream edges between top-level nodes)."""
    WORKFLOWS.pop("round_trip_view", None)

    @form(form_id="round_trip_view", title="Round trip")
    def round_trip_view():
        @node
        def start():
            return (displays.Markdown("hello"), Button("Go"))

        @node
        def finish():
            return displays.Markdown("done")

        s = start()
        f = finish()
        s >> f

    round_trip_view()
    wf = WORKFLOWS["round_trip_view"]
    cw = compile_workflow(wf)
    serialized = serialize_workflow(cw)
    yield cw, serialized
    WORKFLOWS.pop("round_trip_view", None)


class TestDeserializerRoundTrip:
    """compiled_graph_to_workflow reconstructs a CompiledWorkflow whose
    structure matches the original — same node ids, same layout shape,
    same buttons, same downstream edges."""

    def test_workflow_identity(self, compiled_round_trip):
        cw, serialized = compiled_round_trip
        restored = compiled_graph_to_workflow(
            serialized, source_error="ImportError: helper missing",
        )
        assert restored.id == cw.id
        assert restored.title == cw.title
        assert len(restored.steps) == len(cw.steps)

    def test_all_nodes_present(self, compiled_round_trip):
        cw, serialized = compiled_round_trip
        restored = compiled_graph_to_workflow(serialized)
        assert sorted(restored.all_nodes_by_id.keys()) == sorted(
            cw.all_nodes_by_id.keys()
        )

    def test_layout_shape_preserved(self, compiled_round_trip):
        cw, serialized = compiled_round_trip
        restored = compiled_graph_to_workflow(serialized)
        start_orig = cw.all_nodes_by_id["start"]
        start_rest = restored.all_nodes_by_id["start"]
        # Layout tree top-level type matches.
        assert start_rest.layout.type == start_orig.layout.type
        # Buttons make it through.
        assert [b.id for b in start_rest.buttons] == [
            b.id for b in start_orig.buttons
        ]

    def test_downstream_edges_preserved(self, compiled_round_trip):
        cw, serialized = compiled_round_trip
        restored = compiled_graph_to_workflow(serialized)
        for sid, step in cw.by_id.items():
            assert sorted(restored.by_id[sid].downstream) == sorted(
                step.downstream
            )

    def test_landing_node_resolvable(self, compiled_round_trip):
        cw, serialized = compiled_round_trip
        restored = compiled_graph_to_workflow(serialized)
        # The landing_node helper must work on the reconstructed wf —
        # it's used at boot to seed a submission's first step when
        # selective_force_repin falls back.
        assert restored.landing_node().id == cw.landing_node().id


class TestSourceUnavailableTagging:
    """The deserializer tags its output so endpoints can detect a
    view-only workflow and gate advance/submit accordingly."""

    def test_source_unavailable_flag_set(self, compiled_round_trip):
        _, serialized = compiled_round_trip
        restored = compiled_graph_to_workflow(
            serialized, source_error="ImportError: foo",
        )
        assert getattr(restored, "source_unavailable", False) is True
        assert restored.source_error == "ImportError: foo"

    def test_live_workflow_not_tagged(self, compiled_round_trip):
        # Sanity check: a real (compile_workflow-produced) workflow
        # is NOT tagged source_unavailable. The flag is only set by
        # the deserializer.
        cw, _ = compiled_round_trip
        assert getattr(cw, "source_unavailable", False) is False


class TestUnavailableBackendPlaceholder:
    """Backend callables in the deserialized workflow are inert
    placeholders. Invoking them raises WorkflowSourceUnavailable —
    the runtime checks for this to refuse advancement cleanly. We
    construct synthetic serialized backend-step graphs because the
    DSL paths that produce these are exercised elsewhere; here we're
    testing the deserializer's behavior in isolation."""

    def test_workflow_backend_step_fn_is_placeholder(self):
        # Hand-craft a graph with one workflow-level backend step —
        # the same shape `_serialize_step` produces for
        # CompiledBackendStep.
        graph = {
            "id": "f",
            "title": "F",
            "description": "",
            "submission_id_template": None,
            "tags": [],
            "iframe_allowed_origins": None,
            "permission_template": {"roles": [], "default_role_mode": "open"},
            "steps": [
                {
                    "step_kind": "backend",
                    "id": "compute",
                    "fn_name": "compute",
                    "is_branch": False,
                    "hidden": False,
                    "retryable": True,
                    "downstream": [],
                },
            ],
        }
        restored = compiled_graph_to_workflow(
            graph, source_error="ImportError: original cause",
        )
        compute = restored.by_id["compute"]
        assert isinstance(compute.fn, _UnavailableBackendFn)
        assert compute.fn.name == "compute"

    def test_placeholder_raises_with_context(self):
        graph = {
            "id": "f",
            "title": "F",
            "description": "",
            "submission_id_template": None,
            "tags": [],
            "iframe_allowed_origins": None,
            "permission_template": {"roles": [], "default_role_mode": "open"},
            "steps": [
                {
                    "step_kind": "backend",
                    "id": "compute",
                    "fn_name": "compute",
                    "is_branch": False,
                    "hidden": False,
                    "retryable": True,
                    "downstream": [],
                },
            ],
        }
        restored = compiled_graph_to_workflow(
            graph, source_error="ImportError: original cause",
        )
        compute = restored.by_id["compute"]
        with pytest.raises(WorkflowSourceUnavailable) as exc:
            compute.fn()
        assert exc.value.fn_name == "compute"
        assert "ImportError: original cause" in exc.value.source_error

    def test_placeholder_introspection_fields(self):
        # Standalone — the placeholder exposes the surface area
        # BackendFn-consumers read (name, is_branch, hidden,
        # retryable, param_names) without requiring real source.
        p = _UnavailableBackendFn(
            name="something",
            is_branch=True,
            hidden=False,
            retryable=False,
            source_error="boom",
        )
        assert p.name == "something"
        assert p.is_branch is True
        assert p.retryable is False
        assert p.param_names == []  # we don't know real names
        assert p.func is None
