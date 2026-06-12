"""Tests for `widgets.RedistributionEditor` operator validation and
`helpers.apply_redistribution_mapping`.

The operator's job is to refuse bad configs at construction time:
unknown policies, default_policy not in policies, sources/destinations
of the wrong type. The helper's job is to walk a bucket-to-bucket
mapping and dispatch to a form-supplied callback for each chunk of
rows to move — keeping bucket logic in the form, not the framework.

These are unit tests, not frontend tests. The widget's reducer math
lives in TypeScript and is exercised by the frontend bundle tests.
"""
from __future__ import annotations

import pandas as pd
import pytest

from frontflow import widgets
from frontflow.helpers import DROPPED, apply_redistribution_mapping


class TestOperatorValidation:
    """The operator refuses misconfigured DSL constructions early —
    before compile-time, before serve-time — so authors catch
    mistakes immediately.
    """

    def test_basic_construction_works(self):
        # Both policies and default_policy optional — defaults to
        # all policies + manual (so the operation builder renders
        # immediately and the widget's interactive nature is obvious).
        w = widgets.RedistributionEditor(
            id="d",
            data={"a": 10, "b": 20},
            sources=["a"],
            destinations=["b"],
        )
        assert set(w.policies) == {
            "spread_even", "match_shape",
            "push_to_nearest", "manual", "drop",
        }
        assert w.default_policy == "manual"

    def test_explicit_policies_restrict_options(self):
        w = widgets.RedistributionEditor(
            id="d",
            data={"a": 1},
            sources=["a"],
            destinations=[],
            policies=["manual", "drop"],
        )
        assert w.policies == ["manual", "drop"]
        # default_policy stays at "manual" — it's a valid member.
        assert w.default_policy == "manual"

    def test_unknown_policy_rejected(self):
        with pytest.raises(ValueError, match="unknown policies"):
            widgets.RedistributionEditor(
                id="d",
                data={"a": 1},
                sources=["a"],
                destinations=[],
                policies=["bogus"],
            )

    def test_default_policy_not_in_policies_rejected(self):
        # default_policy is "manual" by default. If the form
        # restricts policies to just "drop", the default doesn't fit.
        with pytest.raises(ValueError, match="not in policies"):
            widgets.RedistributionEditor(
                id="d",
                data={"a": 1},
                sources=["a"],
                destinations=[],
                policies=["drop"],  # default_policy="manual" mismatches
            )

    def test_explicit_default_policy_works(self):
        # Pass both — verify the override.
        w = widgets.RedistributionEditor(
            id="d",
            data={"a": 1},
            sources=["a"],
            destinations=[],
            policies=["drop"],
            default_policy="drop",
        )
        assert w.default_policy == "drop"

    def test_empty_policies_rejected(self):
        with pytest.raises(ValueError, match="at least one"):
            widgets.RedistributionEditor(
                id="d",
                data={"a": 1},
                sources=["a"],
                destinations=[],
                policies=[],
            )

    def test_sources_must_be_list_or_stepref(self):
        with pytest.raises(TypeError, match="sources"):
            widgets.RedistributionEditor(
                id="d",
                data={"a": 1},
                sources="not_a_list",  # type: ignore[arg-type]
                destinations=[],
            )

    def test_data_can_be_dict(self):
        # The DSL accepts a literal dict; runtime resolves
        # StepRefs from the steps namespace.
        w = widgets.RedistributionEditor(
            id="d",
            data={"a": 1, "b": 2},
            sources=["a"],
            destinations=["b"],
        )
        assert w.data == {"a": 1, "b": 2}

    def test_data_can_be_list(self):
        # Also accepts the list-of-dicts form for ordering control.
        w = widgets.RedistributionEditor(
            id="d",
            data=[{"key": "a", "count": 1}, {"key": "b", "count": 2}],
            sources=["a"],
            destinations=["b"],
        )
        assert w.data == [
            {"key": "a", "count": 1}, {"key": "b", "count": 2},
        ]


# --- Helper tests --------------------------------------------------------


def _df():
    """Bucket-tagged DataFrame: 6 rows in 'A', 4 rows in 'B'. The
    helper doesn't care what the bucket values are — they could be
    dates, week labels, integers — so the tests use plain letters
    to underline the helper's format-agnosticism."""
    return pd.DataFrame({
        "value": list(range(10)),
        "bucket": ["A"] * 6 + ["B"] * 4,
    })


def _stamp_dest(rows: pd.DataFrame, dest: str) -> pd.DataFrame:
    """Test on_move callback: tag each moved row with the destination
    it landed in. Lets assertions confirm where rows ended up."""
    rows = rows.copy()
    rows["bucket"] = dest
    return rows


class TestApplyRedistributionMapping:
    def test_empty_mapping_is_a_copy(self):
        df = _df()
        out = apply_redistribution_mapping(
            df, {}, bucket_col="bucket", on_move=_stamp_dest,
        )
        pd.testing.assert_frame_equal(out, df)
        # Mutating the result doesn't affect the input.
        out.loc[0, "value"] = 999
        assert df.loc[0, "value"] == 0

    def test_full_redistribution_moves_all_source_rows(self):
        df = _df()
        out = apply_redistribution_mapping(
            df, {"A": {"B": 1.0}},
            bucket_col="bucket",
            on_move=_stamp_dest,
        )
        # 10 rows still present; all A-tagged rows now read B.
        assert len(out) == 10
        assert (out["bucket"] == "B").all()

    def test_dropped_fraction_removes_rows(self):
        df = _df()
        out = apply_redistribution_mapping(
            df, {"A": {DROPPED: 0.5}},
            bucket_col="bucket",
            on_move=_stamp_dest,
        )
        # 50% of 6 A-rows = 3 dropped. 7 rows remain.
        assert len(out) == 7
        # 3 untouched A-rows (the residue from sampling),
        # plus 4 untouched B-rows.
        assert (out["bucket"] == "A").sum() == 3
        assert (out["bucket"] == "B").sum() == 4

    def test_split_destination(self):
        df = _df()
        # Take 50% of A → B, the other 50% → dropped.
        out = apply_redistribution_mapping(
            df, {"A": {"B": 0.5, DROPPED: 0.5}},
            bucket_col="bucket",
            on_move=_stamp_dest,
        )
        # 3 dropped + 3 moved-to-B + 4 already-B = 7 in B, 0 in A.
        assert len(out) == 7
        assert (out["bucket"] == "B").sum() == 7
        assert (out["bucket"] == "A").sum() == 0

    def test_partial_mapping_leaves_residue(self):
        df = _df()
        # 30% of A's 6 rows → B; 70% stays in A.
        out = apply_redistribution_mapping(
            df, {"A": {"B": 0.3}},
            bucket_col="bucket",
            on_move=_stamp_dest,
        )
        assert len(out) == 10
        # int(round(0.3 * 6)) = 2 moved to B.
        assert (out["bucket"] == "B").sum() == 6  # 4 original + 2 moved
        assert (out["bucket"] == "A").sum() == 4  # residue

    def test_on_move_receives_correct_rows_and_dest(self):
        """The callback gets the exact slice of rows for each
        destination, plus the destination key — proving the
        bookkeeping passes the right chunks to the right places."""
        df = _df()
        calls = []

        def record(rows, dest):
            calls.append((list(rows["value"]), dest))
            return _stamp_dest(rows, dest)

        # Split A's 6 rows: 50% to B (3 rows), 50% to C (3 rows).
        # Source rows are taken head-first, so B gets values
        # [0,1,2] and C gets [3,4,5].
        apply_redistribution_mapping(
            df, {"A": {"B": 0.5, "C": 0.5}},
            bucket_col="bucket",
            on_move=record,
        )
        assert calls == [([0, 1, 2], "B"), ([3, 4, 5], "C")]

    def test_callback_can_use_dest_as_a_date_string(self):
        """The dest key is opaque to the helper — but a realistic
        form will use it as a date and mutate a date column. Smoke
        test that pattern."""
        df = pd.DataFrame({
            "date": pd.to_datetime(["2024-01-08", "2024-01-09"]),
            "bucket": ["2024-01-08", "2024-01-08"],
        })

        def move_to_dest(rows, dest):
            rows = rows.copy()
            rows["date"] = pd.Timestamp(dest)
            return rows

        out = apply_redistribution_mapping(
            df, {"2024-01-08": {"2024-01-15": 1.0}},
            bucket_col="bucket",
            on_move=move_to_dest,
        )
        assert (out["date"] == pd.Timestamp("2024-01-15")).all()

    def test_uniform_full_redistribution_preserves_row_count(self):
        """Hamilton apportionment regression. spread_even-style
        uniform fractions (e.g. 1/3, 1/3, 1/3) used to leak rows
        when the source count wasn't divisible by the destination
        count — naive `round(2.33)*3 = 6` for a 7-row source. The
        helper now does Hamilton: floor + redistribute residue,
        so the per-source total always matches the policy's intent.

        Concretely for spread_even: a source whose fractions sum
        to 1.0 must claim every one of its rows."""
        df = pd.DataFrame({
            "bucket": ["A"] * 7 + ["B", "C", "D"],
            "value": list(range(10)),
        })
        # 7 source rows, 3 equal destinations.
        out = apply_redistribution_mapping(
            df, {"A": {"B": 1/3, "C": 1/3, "D": 1/3}},
            bucket_col="bucket",
            on_move=_stamp_dest,
        )
        # All 10 rows still present — no residue lost.
        assert len(out) == 10
        # Every source row was actually moved off A.
        assert (out["bucket"] == "A").sum() == 0
        # 7 source rows split via Hamilton: 3+2+2. Layered on the
        # original 1+1+1 in B/C/D, final counts sort to [3, 3, 4].
        counts = out.groupby("bucket").size()
        assert sorted(counts.tolist()) == [3, 3, 4]

    def test_skewed_full_redistribution_preserves_row_count(self):
        """Same invariant for non-uniform fractions: a mapping whose
        per-source fractions sum to 1.0 claims every row, regardless
        of how the floor/ceiling splits land."""
        df = pd.DataFrame({
            "bucket": ["A"] * 11 + ["B", "C", "D"],
            "value": list(range(14)),
        })
        # match_shape-ish: weights summing to 1.
        out = apply_redistribution_mapping(
            df, {"A": {"B": 9/11, "C": 1/11, "D": 1/11}},
            bucket_col="bucket",
            on_move=_stamp_dest,
        )
        assert len(out) == 14
        assert (out["bucket"] == "A").sum() == 0

    def test_two_policies_agree_on_total_row_count(self):
        """Different policies might land rows in different
        destinations, but the TOTAL count of moved rows must be
        identical when both mappings represent a full
        redistribution (fractions sum to 1.0). This is the bug
        Connor reported: spread_even and match_shape on the same
        data produced different KPI totals because rounding leaked
        rows asymmetrically."""
        df = pd.DataFrame({
            "bucket": ["A"] * 7 + ["B"] * 5 + ["C"] * 2 + ["D"] * 1,
            "value": list(range(15)),
        })
        # spread_even: each source distributes 1/3 to each dest
        spread = {"A": {"B": 1/3, "C": 1/3, "D": 1/3}}
        # match_shape: weighted by dest counts (5/8, 2/8, 1/8)
        match = {"A": {"B": 5/8, "C": 2/8, "D": 1/8}}

        spread_out = apply_redistribution_mapping(
            df.copy(), spread, bucket_col="bucket", on_move=_stamp_dest,
        )
        match_out = apply_redistribution_mapping(
            df.copy(), match, bucket_col="bucket", on_move=_stamp_dest,
        )
        # Both must conserve row count.
        assert len(spread_out) == len(match_out) == 15
        # Both must clear A entirely (full redistribution).
        assert (spread_out["bucket"] == "A").sum() == 0
        assert (match_out["bucket"] == "A").sum() == 0

    def test_partial_allocation_still_keeps_residue(self):
        """Hamilton is only used to spread the rounding residue —
        if the user's fractions sum to less than 1.0, the helper
        still leaves the unallocated portion in the source bucket
        as before. Confirms the fix didn't break the manual-policy
        contract."""
        df = pd.DataFrame({
            "bucket": ["A"] * 10,
            "value": list(range(10)),
        })
        # 30% to B; 70% should stay in A.
        out = apply_redistribution_mapping(
            df, {"A": {"B": 0.3}},
            bucket_col="bucket",
            on_move=_stamp_dest,
        )
        assert len(out) == 10
        assert (out["bucket"] == "B").sum() == 3
        assert (out["bucket"] == "A").sum() == 7
