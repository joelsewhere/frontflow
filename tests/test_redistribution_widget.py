"""End-to-end test: a form using widgets.RedistributionEditor compiles
to a block with the expected shape, the runtime serves the block with
literal data intact, and the persisted submission can round-trip a
mapping value.

These are runtime-level tests — they go through the same code path as
production traffic (compile + resolve_block) but use the in-memory
FORMS registry rather than HTTP.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def redist_workflow(app):
    """The test_redistribution_editor compiled workflow."""
    import frontflow.main as main_mod
    wf = main_mod.FORMS.get("test_redistribution_editor")
    assert wf is not None, (
        "test_redistribution_editor fixture form missing from FORMS"
    )
    return wf


class TestCompileRedistributionEditor:
    def test_block_compiles_with_expected_type_and_props(
        self, redist_workflow
    ):
        """The DSL operator should produce a `redistribution_widget`
        block whose props carry the policies, default_policy, sources,
        destinations, and (literal) data."""
        # Walk the compiled blocks to find the redistribution one.
        configure = redist_workflow.all_nodes_by_id["configure"]
        block = configure.layout

        def find_redist(b):
            if b.type == "redistribution_widget":
                return b
            for child in b.children:
                hit = find_redist(child)
                if hit is not None:
                    return hit
            return None

        target = find_redist(block)
        assert target is not None, "no redistribution_widget block emitted"

        props = target.props
        # Fixture uses defaults — all 5 policies and match_shape.
        assert set(props["policies"]) == {
            "spread_even", "match_shape",
            "push_to_nearest", "manual", "drop",
        }
        assert props["default_policy"] == "manual"
        assert props["sources"] == ["2024-W48", "2024-W49"]
        assert props["destinations"] == ["2024-W01", "2024-W02"]
        # Literal `data` baked into the compiled block (no data_from).
        assert "data_from" not in props
        assert props["data"] == {
            "2024-W01": 10, "2024-W02": 15,
            "2024-W48": 3, "2024-W49": 5,
        }

    def test_submission_can_persist_a_mapping_value(self, redist_workflow):
        """A redistribution-editor field accepts an arbitrary JSON
        object as its submitted value. The runtime stores it
        verbatim — same as histogram_widget."""
        from frontflow.dsl import runtime

        # Build a realistic widget output: manual policy with two
        # explicit operations.
        value = {
            "policy": "manual",
            "operations": [
                {
                    "sources": ["2024-W48"],
                    "destinations": [],
                    "fraction": 1.0,
                    "shape": "match",
                },
                {
                    "sources": ["2024-W49"],
                    "destinations": ["2024-W01"],
                    "fraction": 1.0,
                    "shape": "match",
                },
            ],
            "mapping": {
                "2024-W48": {"_dropped": 1.0},
                "2024-W49": {"2024-W01": 1.0},
            },
        }

        sub = runtime.start_submission(
            redist_workflow, {"distribute": value}
        )
        # The submitted step records the value in form_values
        # verbatim; advance() runs the (empty) chain to terminal.
        runtime.advance(redist_workflow, sub)
        assert sub.terminated is True
        # Round-tripped value lives on the configure step.
        configure_step = next(
            s for s in sub.steps if s.node_id == "configure"
        )
        assert configure_step.form_values["distribute"] == value
