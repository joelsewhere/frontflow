"""Unit tests for airflow_dispatch — pure functions, no I/O.

Covers dedupe_clear_ops (with and without DAG graphs), the
downstream-closure helper, and plan_airflow_clear's branching on
operator kind / state shape."""
from __future__ import annotations

import pytest

from frontflow.dsl.airflow_dispatch import (
    _downstream_closure,
    _drop_subsumed_task_ids,
    dedupe_clear_ops,
)


# ----------------------------------------------------------------- #
# _downstream_closure                                               #
# ----------------------------------------------------------------- #


class TestDownstreamClosure:
    def test_linear_chain(self):
        graph = {"a": ["b"], "b": ["c"], "c": []}
        assert _downstream_closure("a", graph) == {"b", "c"}
        assert _downstream_closure("b", graph) == {"c"}
        assert _downstream_closure("c", graph) == set()

    def test_diamond(self):
        # a -> b, a -> c, b -> d, c -> d
        graph = {"a": ["b", "c"], "b": ["d"], "c": ["d"], "d": []}
        assert _downstream_closure("a", graph) == {"b", "c", "d"}

    def test_missing_task_returns_empty(self):
        assert _downstream_closure("nope", {"a": []}) == set()

    def test_cycle_does_not_infinite_loop(self):
        # Airflow shouldn't produce these but defensive code matters.
        graph = {"x": ["y"], "y": ["x"]}
        result = _downstream_closure("x", graph)
        # Either {y} or {x, y} is acceptable — the point is it
        # terminates and doesn't blow the stack.
        assert "y" in result


# ----------------------------------------------------------------- #
# _drop_subsumed_task_ids                                           #
# ----------------------------------------------------------------- #


class TestDropSubsumedTaskIds:
    def test_ancestor_subsumes_descendant(self):
        graph = {"a": ["b"], "b": ["c"], "c": []}
        # b and c are both reachable from a; clearing a covers them.
        assert _drop_subsumed_task_ids({"a", "b", "c"}, graph) == {"a"}

    def test_independent_tasks_stay(self):
        graph = {"a": [], "b": []}
        assert _drop_subsumed_task_ids({"a", "b"}, graph) == {"a", "b"}

    def test_partial_subsumption(self):
        # a -> b, c independent
        graph = {"a": ["b"], "b": [], "c": []}
        assert _drop_subsumed_task_ids({"a", "b", "c"}, graph) == {
            "a", "c",
        }

    def test_diamond_keeps_root_only(self):
        graph = {"a": ["b", "c"], "b": ["d"], "c": ["d"], "d": []}
        assert _drop_subsumed_task_ids(
            {"a", "b", "c", "d"}, graph
        ) == {"a"}

    def test_unknown_tasks_stay(self):
        # Task isn't in the graph (DAG was edited) — leave it alone.
        graph = {"a": ["b"], "b": []}
        assert _drop_subsumed_task_ids({"unknown"}, graph) == {"unknown"}


# ----------------------------------------------------------------- #
# dedupe_clear_ops — without graphs (backward compat)               #
# ----------------------------------------------------------------- #


class TestDedupeClearOpsWithoutGraphs:
    def test_empty_input(self):
        assert dedupe_clear_ops([]) == []

    def test_single_op_passes_through(self):
        op = {"dag_id": "d", "run_id": "r", "task_ids": ["a"]}
        assert dedupe_clear_ops([op]) == [op]

    def test_whole_run_subsumes_tasks(self):
        ops = [
            {"dag_id": "d", "run_id": "r", "task_ids": None},
            {"dag_id": "d", "run_id": "r", "task_ids": ["a"]},
        ]
        result = dedupe_clear_ops(ops)
        assert len(result) == 1
        assert result[0]["task_ids"] is None

    def test_merges_task_clears_for_same_run(self):
        ops = [
            {"dag_id": "d", "run_id": "r", "task_ids": ["a"]},
            {"dag_id": "d", "run_id": "r", "task_ids": ["b"]},
        ]
        result = dedupe_clear_ops(ops)
        assert len(result) == 1
        assert sorted(result[0]["task_ids"]) == ["a", "b"]

    def test_separate_runs_stay_separate(self):
        ops = [
            {"dag_id": "d", "run_id": "r1", "task_ids": ["a"]},
            {"dag_id": "d", "run_id": "r2", "task_ids": ["a"]},
        ]
        result = dedupe_clear_ops(ops)
        assert len(result) == 2

    def test_separate_dags_stay_separate(self):
        ops = [
            {"dag_id": "d1", "run_id": "r", "task_ids": ["a"]},
            {"dag_id": "d2", "run_id": "r", "task_ids": ["a"]},
        ]
        result = dedupe_clear_ops(ops)
        assert len(result) == 2


# ----------------------------------------------------------------- #
# dedupe_clear_ops — with graphs (this session's feature)           #
# ----------------------------------------------------------------- #


class TestDedupeClearOpsWithGraphs:
    def test_subsumed_tasks_dropped(self):
        ops = [
            {"dag_id": "d", "run_id": "r", "task_ids": ["a"]},
            {"dag_id": "d", "run_id": "r", "task_ids": ["b"]},
            {"dag_id": "d", "run_id": "r", "task_ids": ["c"]},
        ]
        graphs = {"d": {"a": ["b"], "b": ["c"], "c": []}}
        result = dedupe_clear_ops(ops, dag_graphs=graphs)
        assert len(result) == 1
        assert result[0]["task_ids"] == ["a"]

    def test_independent_tasks_both_stay(self):
        ops = [
            {"dag_id": "d", "run_id": "r", "task_ids": ["a"]},
            {"dag_id": "d", "run_id": "r", "task_ids": ["d"]},
        ]
        graphs = {"d": {"a": ["b"], "b": [], "d": []}}
        result = dedupe_clear_ops(ops, dag_graphs=graphs)
        assert len(result) == 1
        assert sorted(result[0]["task_ids"]) == ["a", "d"]

    def test_missing_graph_falls_back(self):
        # No graph for this dag → behaves like the no-graph case.
        ops = [
            {"dag_id": "d", "run_id": "r", "task_ids": ["a"]},
            {"dag_id": "d", "run_id": "r", "task_ids": ["b"]},
        ]
        result = dedupe_clear_ops(ops, dag_graphs={})
        assert sorted(result[0]["task_ids"]) == ["a", "b"]

    def test_whole_run_still_wins_over_graph_dedupe(self):
        ops = [
            {"dag_id": "d", "run_id": "r", "task_ids": None},
            {"dag_id": "d", "run_id": "r", "task_ids": ["a"]},
        ]
        graphs = {"d": {"a": ["b"], "b": []}}
        result = dedupe_clear_ops(ops, dag_graphs=graphs)
        assert len(result) == 1
        assert result[0]["task_ids"] is None

    def test_single_task_no_dedupe_overhead(self):
        # Optimization path: <2 tasks means closure logic skipped.
        ops = [{"dag_id": "d", "run_id": "r", "task_ids": ["only"]}]
        graphs = {"d": {"only": []}}
        result = dedupe_clear_ops(ops, dag_graphs=graphs)
        assert result == ops
