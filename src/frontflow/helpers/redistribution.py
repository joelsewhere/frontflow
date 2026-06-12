"""Apply a `widgets.RedistributionEditor` mapping to a DataFrame.

The widget produces a bucket-to-bucket mapping; this helper walks
that mapping and dispatches to a form-provided callback for each
(rows, destination_bucket) pair. **The helper does no bucketing
itself** — bucket generation, what "a row belongs to bucket X"
means, and what "move these rows to bucket X" means are all the
form's job. That keeps the helper format-agnostic, matching the
widget.

Form's responsibilities:
  - Tag each row with its source bucket key (a column on the df)
  - Define an `on_move(rows, dest_bucket_key) -> rows` callback that
    mutates the rows however "move to this bucket" should mean for
    the form's data (assigning a date inside the bucket's span,
    updating a category column, etc.)

Helper's job:
  - Walk the mapping
  - For each `(source, dest, fraction)` triple, take the right
    slice of source rows (in their existing order, head-first)
  - Dispatch to `on_move` for redistribution, or drop entirely
    for the `_dropped` marker
  - Combine the kept + moved + untouched residue into the result

Use it like:

    @backend
    def transform(steps):
        df = read_csv(...)
        # Form-defined bucketing — Monday of the ISO week as a
        # YYYY-MM-DD string, since that's what extract_counts
        # produced upstream.
        df["bucket"] = (
            df["Lease - Completed"]
            .dt.to_period("W")
            .dt.start_time
            .dt.strftime("%Y-%m-%d")
        )

        def assign_to_week(rows, dest_bucket):
            # Spread N rows uniformly across the destination week.
            start = pd.Timestamp(dest_bucket)
            n = len(rows)
            step = pd.Timedelta(days=7) / (n + 1)
            rows = rows.copy()
            rows["Lease - Completed"] = [
                start + step * (i + 1) for i in range(n)
            ]
            return rows

        df = apply_redistribution_mapping(
            df,
            steps.distribute.mapping["mapping"],
            bucket_col="bucket",
            on_move=assign_to_week,
        )
        df = df.drop(columns=["bucket"])
        return df
"""

from __future__ import annotations

from typing import Callable

import pandas as pd

# Reserved destination key inside a mapping's inner dict, meaning
# "discard this fraction of records." Forms importing the helper
# can match against this constant rather than the literal string.
DROPPED = "_dropped"


def apply_redistribution_mapping(
    df: pd.DataFrame,
    mapping: dict[str, dict[str, float]],
    *,
    bucket_col: str,
    on_move: Callable[[pd.DataFrame, str], pd.DataFrame],
) -> pd.DataFrame:
    """Walk a redistribution mapping, sample rows from each source
    bucket, and dispatch to `on_move` for each redistribution.

    Args:
      df: input DataFrame. Not mutated; the result is a new frame.
      mapping: `{source_bucket: {dest_bucket | "_dropped": fraction}}`
        as the widget produces it. Empty mapping is a no-op (the
        original df is returned as a copy).
      bucket_col: name of a DataFrame column whose values are the
        source bucket keys. The form must populate this column
        before calling — the helper makes no assumptions about
        how rows are bucketed.
      on_move: called with `(rows_subset, dest_bucket_key)`; returns
        the rows with whatever mutations the form wants. Typical
        implementation: rewrite a date column so the rows land
        inside the destination bucket's span. The returned frame
        must have the same index as `rows_subset` (the helper
        uses those indices to splice the moved rows back into the
        output).

    Returns the updated DataFrame. Rows are kept in their original
    index order; `_dropped` rows are removed.

    Sampling: rows are taken in the order they appear in `df`.
    Per-source integer counts are computed via Hamilton
    apportionment — each destination's `floor(fraction * n_src)` is
    the starting allocation, then the rounding residue is dealt to
    destinations with the largest fractional parts until the per-
    source total matches `round(sum_of_fractions * n_src)`. This
    keeps the policy's intent — fractions summing to 1.0 always
    claim every source row; partial allocations leave the correct
    integer count behind as residue in the source bucket. Avoids the
    "naive per-dest rounding can lose rows" bug that bit spread_even
    and similar uniform-fraction policies.

    Forms wanting random sampling instead of head-first should
    pre-shuffle `df` before calling.
    """
    if not mapping:
        return df.copy()

    # Rows we'll drop entirely (mapped to DROPPED).
    drop_idx: list = []
    # Rows whose data we'll replace, collected as a list of frames
    # so we can concat once at the end rather than mutating
    # row-by-row.
    moved_frames: list[pd.DataFrame] = []

    for src_bucket, dests in mapping.items():
        src_rows_idx = df.index[df[bucket_col] == src_bucket]
        if len(src_rows_idx) == 0:
            continue
        n_src = len(src_rows_idx)
        items = list(dests.items())

        # Hamilton apportionment so per-dest integer counts sum
        # back to the rounded-total — naive `round(fraction *
        # n_src)` per dest gives a residue that differs between
        # policies (e.g. spread_even at fractions 1/3, 1/3, 1/3
        # on 7 rows: round each = 2 → only 6 of 7 rows claimed).
        # Hamilton: floor each, then add +1 to the destinations
        # with the largest fractional parts until the sum matches
        # the policy's target.
        #
        # The target itself respects partial allocations — a
        # source whose fractions sum to 0.7 still keeps 30% as
        # untouched residue in the source bucket (the manual
        # policy uses this for "leave some put"). Full-allocation
        # mappings (sum ≈ 1.0) claim every row.
        exact = [f * n_src for _, f in items]
        counts = [int(e) for e in exact]  # floor for non-negative
        target = int(round(sum(exact)))
        shortfall = target - sum(counts)
        if shortfall > 0:
            fractional = [e - int(e) for e in exact]
            ranked = sorted(
                range(len(items)), key=lambda i: -fractional[i]
            )
            for i in ranked[:shortfall]:
                counts[i] += 1

        cursor = 0
        for (dest_key, _), n in zip(items, counts):
            if n == 0:
                continue
            claimed_idx = src_rows_idx[cursor : cursor + n]
            cursor += n
            if dest_key == DROPPED:
                drop_idx.extend(claimed_idx.tolist())
                continue
            moved = on_move(df.loc[claimed_idx], dest_key)
            moved_frames.append(moved)

    if not moved_frames and not drop_idx:
        return df.copy()

    moved_idx_set: set = set()
    for f in moved_frames:
        moved_idx_set.update(f.index.tolist())
    touched_idx = moved_idx_set.union(drop_idx)

    # Untouched rows pass through. Moved rows replace their
    # originals at the same indices. Dropped rows are excluded.
    untouched = df.loc[~df.index.isin(touched_idx)]
    if moved_frames:
        moved_all = pd.concat(moved_frames)
        out = pd.concat([untouched, moved_all]).sort_index()
    else:
        out = untouched.copy()
    return out
