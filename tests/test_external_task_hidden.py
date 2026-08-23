"""`hidden=True` on a chain step has to survive being persisted.

A form_version stores a SERIALIZED graph, and every later load
deserializes that rather than re-reading the DSL. So a flag the compiler
sets but the serializer drops works exactly once — in the process that
compiled it — and is gone from then on.

That is how a filter panel grew a "STEP 03 · COMPLETE" card: the
SetFilters step was declared `hidden=True`, compiled correctly, written
without the flag, read back as `hidden=False`, and drawn.

The flag cannot be dropped server-side instead: the step's directives
ride on its task payload, so omitting the task stops it doing its job
rather than just showing it.
"""

from __future__ import annotations

import pytest

from frontflow.dsl.compile import (
    CompiledExternalTask,
    compiled_graph_to_workflow,
    compile_workflow,
    serialize_workflow,
)


def _external_tasks(graph: dict) -> list[dict]:
    return [t for step in graph["steps"] for t in step.get("external_tasks") or []]


class TestHiddenSurvivesSerialization:
    def test_a_hidden_chain_step_round_trips(self):
        from frontflow import Button, displays, form, node
        from frontflow.superset import SetFilters

        @form(form_id="test_hidden_chain", title="Hidden chain")
        def _hidden_chain():
            @node
            def only():
                go = Button("Go")
                go >> SetFilters("dash", hidden=True, Region="x")
                return displays.Column(go)

            only()

        # A @form registers when it is CALLED, not when it is defined.
        _hidden_chain()

        from frontflow.dsl.core import WORKFLOWS

        graph = serialize_workflow(compile_workflow(WORKFLOWS["test_hidden_chain"]))

        tasks = _external_tasks(graph)
        assert tasks, "the chain step should be in the graph at all"
        assert tasks[0]["hidden"] is True, "hidden must be written"

        restored = compiled_graph_to_workflow(graph)
        task = restored.nodes[0].external_tasks[0]
        assert task.hidden is True, "hidden must be read back"

    def test_a_visible_chain_step_stays_visible(self):
        """The over-reach guard: this must not hide everything."""
        from frontflow import Button, displays, form, node
        from frontflow.superset import SetFilters

        @form(form_id="test_visible_chain", title="Visible chain")
        def _visible_chain():
            @node
            def only():
                go = Button("Go")
                go >> SetFilters("dash", Region="x")
                return displays.Column(go)

            only()

        _visible_chain()

        from frontflow.dsl.core import WORKFLOWS

        graph = serialize_workflow(compile_workflow(WORKFLOWS["test_visible_chain"]))
        assert _external_tasks(graph)[0]["hidden"] is False

        restored = compiled_graph_to_workflow(graph)
        assert restored.nodes[0].external_tasks[0].hidden is False


class TestOlderGraphs:
    def test_a_graph_written_before_the_flag_reads_as_visible(self):
        """Graphs outlive the code that wrote them. Absent means the
        author never asked for it — which is False, not a crash."""
        task = CompiledExternalTask(task_id="t", kind="k", config={})
        assert task.hidden is False

    @pytest.mark.parametrize("stored", [None, {}, {"graph_visible": True}])
    def test_deserializing_without_the_key_is_safe(self, stored):
        payload = {
            "id": "n",
            "title": "n",
            "layout": {"type": "column", "props": {}, "children": []},
            "external_tasks": [
                {"task_id": "t", "kind": "k", "config": {}, **(stored or {})}
            ],
        }
        from frontflow.dsl.compile import _deserialize_node

        node = _deserialize_node(payload, None)
        assert node.external_tasks[0].hidden is False
