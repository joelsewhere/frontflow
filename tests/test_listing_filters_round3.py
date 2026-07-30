"""Tests for the round-3 listing filters:
  - `show_deleted=1` (admin-only) includes tombstoned rows AND
    returns `deleted_at` on each row
  - `updated_since` / `updated_before` half-open interval on
    `Submission.updated_at`, accepting calendar dates OR ISO
    datetimes
  - `current_step=node_id` multi-select filter, backed by a
    window-ranked subquery
  - `/forms/{id}/submissions/current-steps` populates the filter
    dropdown with node_id + count
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import frontflow.dsl.store as store
from frontflow.dsl.store import (
    Session, Submission, _engine,
    soft_delete_submissions_by_handles,
)
from frontflow.main import _parse_listing_bound


def _start(client: TestClient, name: str = "x") -> dict:
    r = client.post(
        "/api/forms/test_simple/submissions",
        json={"values": {"name": name, "note": "ok"}},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _listing(
    client: TestClient,
    **kwargs,
) -> dict:
    params: list[tuple[str, str]] = []
    for k, v in kwargs.items():
        if v is None:
            continue
        if isinstance(v, list):
            for item in v:
                params.append((k, str(item)))
        elif isinstance(v, bool):
            if v:
                params.append((k, "1"))
        else:
            params.append((k, str(v)))
    r = client.get(
        "/api/forms/test_simple/submissions", params=params,
    )
    assert r.status_code == 200, r.text
    return r.json()


# --- Show-deleted ----------------------------------------------------------


class TestShowDeleted:
    def test_default_hides_deleted_rows(
        self, app, admin_client: TestClient,
    ):
        """The listing without `show_deleted` filters out tombstoned
        rows — the default tab view should be tombstone-free."""
        live = _start(admin_client, "live")
        gone = _start(admin_client, "gone")
        soft_delete_submissions_by_handles(
            [gone["handle"]], form_id="test_simple",
        )
        page = _listing(admin_client)
        handles = {r["handle"] for r in page["submissions"]}
        assert live["handle"] in handles
        assert gone["handle"] not in handles

    def test_show_deleted_admin_returns_tombstones(
        self, app, admin_client: TestClient,
    ):
        """Admin + `show_deleted=1` → tombstoned rows surface with a
        non-null `deleted_at` field. The UI uses that to render the
        "deleted" pill and disable navigation."""
        live = _start(admin_client, "live")
        gone = _start(admin_client, "gone")
        soft_delete_submissions_by_handles(
            [gone["handle"]], form_id="test_simple",
        )
        page = _listing(admin_client, show_deleted=True)
        by_handle = {r["handle"]: r for r in page["submissions"]}
        assert live["handle"] in by_handle
        assert gone["handle"] in by_handle
        # Tombstone field surfaces only on the deleted row.
        assert by_handle[live["handle"]]["deleted_at"] is None
        assert by_handle[gone["handle"]]["deleted_at"] is not None

    def test_show_deleted_non_admin_silently_ignored(
        self, app, admin_client: TestClient,
        user_client: TestClient,
        admin_user: dict, regular_user: dict,
    ):
        """A non-admin passing `show_deleted=1` does NOT bypass the
        filter — defense in depth on top of the UI gate. The flag
        is admin-only; a hand-crafted URL by a regular user gets
        the same view as without the flag.

        The `admin_client` and `user_client` fixtures share a
        cookie jar (see test_route_auth.py's note); whichever
        logged in most recently owns the session. We re-login the
        admin before creating rows so they're admin-owned, then
        re-login the regular user before the assertion.
        """
        admin_client.post("/api/auth/login", json=admin_user)
        live = _start(admin_client, "live")
        gone = _start(admin_client, "gone")
        soft_delete_submissions_by_handles(
            [gone["handle"]], form_id="test_simple",
        )
        # Switch the jar back to the regular user.
        user_client.post("/api/auth/login", json=regular_user)
        r = user_client.get(
            "/api/forms/test_simple/submissions",
            params=[("show_deleted", "1")],
        )
        assert r.status_code == 200, r.text
        handles = {row["handle"] for row in r.json()["submissions"]}
        # Both rows are admin-created on a public form — a stranger
        # has no step-submitter / assignee / granter relationship,
        # so the visibility filter returns empty. The point of the
        # assertion is that `show_deleted=1` couldn't widen that
        # — the deleted row stays out even when the URL asks for
        # it. (If show_deleted were leaking through, the live row
        # would ALSO appear, which we explicitly assert against.)
        assert live["handle"] not in handles
        assert gone["handle"] not in handles  # nor the deleted one


# --- Date-window filter ----------------------------------------------------


class TestDateWindow:
    def test_updated_since_excludes_earlier_rows(
        self, app, admin_client: TestClient,
    ):
        """`updated_since` filters rows where `updated_at < since`
        out of the result. Calendar-date input is interpreted as
        start-of-day UTC."""
        from sqlalchemy import update as _update
        old = _start(admin_client, "old")
        new = _start(admin_client, "new")
        # Force `updated_at` to a known past date on `old`.
        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        with Session(_engine) as s:
            s.execute(
                _update(Submission)
                .where(Submission.handle == old["handle"])
                .values(updated_at=past)
            )
            s.commit()

        page = _listing(admin_client, updated_since="2024-01-01")
        handles = {r["handle"] for r in page["submissions"]}
        assert new["handle"] in handles
        assert old["handle"] not in handles

    def test_updated_before_is_half_open_on_calendar_date(
        self, app, admin_client: TestClient,
    ):
        """A single-day window — `updated_since=DATE&updated_before=
        DATE` — captures everything stamped on that day. The
        endpoint bumps `before` to start-of-next-day so the day's
        own rows aren't accidentally excluded by a strict-less-than
        upper bound."""
        from sqlalchemy import update as _update
        row = _start(admin_client, "same-day")
        # Put the row's updated_at in the middle of June 15th.
        on_day = datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc)
        with Session(_engine) as s:
            s.execute(
                _update(Submission)
                .where(Submission.handle == row["handle"])
                .values(updated_at=on_day)
            )
            s.commit()
        page = _listing(
            admin_client,
            updated_since="2024-06-15",
            updated_before="2024-06-15",
        )
        handles = {r["handle"] for r in page["submissions"]}
        assert row["handle"] in handles

    def test_iso_datetime_bound_accepted_too(
        self, app, admin_client: TestClient,
    ):
        """A full ISO datetime bound — what external integrations
        and the future unified endpoint hand in — parses too. Same
        function on both ends."""
        from sqlalchemy import update as _update
        in_window = _start(admin_client, "in")
        too_old = _start(admin_client, "out")
        ancient = datetime(2010, 1, 1, tzinfo=timezone.utc)
        with Session(_engine) as s:
            s.execute(
                _update(Submission)
                .where(Submission.handle == too_old["handle"])
                .values(updated_at=ancient)
            )
            s.commit()
        page = _listing(
            admin_client,
            updated_since="2024-01-01T00:00:00Z",
        )
        handles = {r["handle"] for r in page["submissions"]}
        assert in_window["handle"] in handles
        assert too_old["handle"] not in handles

    def test_malformed_date_silently_dropped(
        self, app, admin_client: TestClient,
    ):
        """A garbage bound (`?updated_since=banana`) doesn't 500 the
        listing — same posture as the other filters. The parse
        returns None, the bound goes unset, the user sees all
        rows."""
        _start(admin_client, "still-here")
        page = _listing(admin_client, updated_since="banana")
        assert page["total"] >= 1


class TestParseListingBound:
    """Direct tests on the bound parser — easier to cover edge
    cases here than through end-to-end listing calls."""

    def test_empty_string_is_none(self):
        assert _parse_listing_bound(None, end_of_day=False) is None
        assert _parse_listing_bound("", end_of_day=False) is None
        assert _parse_listing_bound("   ", end_of_day=False) is None

    def test_calendar_date_start_of_day(self):
        dt = _parse_listing_bound(
            "2025-06-30", end_of_day=False,
        )
        assert dt == datetime(2025, 6, 30, tzinfo=timezone.utc)

    def test_calendar_date_end_of_day_bumps_to_next_day(self):
        """`end_of_day=True` on a calendar date returns start-of-the
        -NEXT-day in UTC. That's how a same-day window catches
        rows stamped during the day."""
        dt = _parse_listing_bound(
            "2025-06-30", end_of_day=True,
        )
        assert dt == datetime(2025, 7, 1, tzinfo=timezone.utc)

    def test_iso_datetime_passes_through(self):
        dt = _parse_listing_bound(
            "2025-06-30T13:45:00+00:00", end_of_day=False,
        )
        assert dt == datetime(
            2025, 6, 30, 13, 45, tzinfo=timezone.utc,
        )

    def test_z_suffix_normalized(self):
        """Python 3.10 doesn't accept the `Z` suffix in
        `fromisoformat`; we pre-normalize so external integrations
        that emit `Z` aren't gratuitously rejected."""
        dt = _parse_listing_bound(
            "2025-06-30T13:45:00Z", end_of_day=False,
        )
        assert dt == datetime(
            2025, 6, 30, 13, 45, tzinfo=timezone.utc,
        )

    def test_garbage_returns_none(self):
        assert (
            _parse_listing_bound("banana", end_of_day=False) is None
        )
        assert (
            _parse_listing_bound("2025-13-99", end_of_day=False)
            is None
        )


# --- Current-step filter ---------------------------------------------------


class TestCurrentStepFilter:
    def test_filters_by_current_step(
        self, app, admin_client: TestClient,
    ):
        """`current_step=NODE_ID` returns only submissions whose
        last Step row's node_id matches. Uses the window-ranked
        subquery — the listing picks the same "last step" the
        runtime would for `Submission.steps[-1]`."""
        # test_simple has a single node; the submission auto-
        # terminates with the only step being that node. So all
        # submissions share the same current_step. Verify by
        # querying for it AND by querying for a bogus one.
        _start(admin_client, "a")
        _start(admin_client, "b")
        # First, look up what node id the form's only step is.
        page = _listing(admin_client)
        first = page["submissions"][0]
        node = first["current_step"]
        assert node is not None

        hit = _listing(admin_client, current_step=[node])
        assert hit["total"] == 2

        miss = _listing(admin_client, current_step=["bogus_node"])
        assert miss["total"] == 0

    def test_current_step_multi_select_union(
        self, app, admin_client: TestClient,
    ):
        """Multiple `current_step=` params act as a set union — a
        row matches if its node is in ANY of them. Same shape as
        `state` multi-select."""
        _start(admin_client, "a")
        page0 = _listing(admin_client)
        node = page0["submissions"][0]["current_step"]
        # Union of {node, bogus} — still includes the row.
        page = _listing(
            admin_client, current_step=[node, "bogus_node"],
        )
        assert page["total"] >= 1


# --- /current-steps endpoint -----------------------------------------------


class TestCurrentStepsEndpoint:
    def test_returns_node_ids_with_counts(
        self, app, admin_client: TestClient,
    ):
        """The dropdown-options endpoint returns `[{node_id, count}]`
        ordered by count desc — most-common steps first so they're
        discoverable at the top of the dropdown."""
        _start(admin_client, "a")
        _start(admin_client, "b")
        _start(admin_client, "c")
        r = admin_client.get(
            "/api/forms/test_simple/submissions/current-steps",
        )
        assert r.status_code == 200, r.text
        opts = r.json()
        assert len(opts) == 1  # one node in test_simple
        assert opts[0]["count"] == 3
        assert "node_id" in opts[0]

    def test_excludes_deleted_by_default(
        self, app, admin_client: TestClient,
    ):
        """A soft-deleted submission must not contribute to the
        dropdown counts — otherwise the dropdown would suggest
        filtering on a step nobody's currently on (from the user's
        point of view)."""
        live = _start(admin_client, "live")
        gone = _start(admin_client, "gone")
        soft_delete_submissions_by_handles(
            [gone["handle"]], form_id="test_simple",
        )
        del live
        r = admin_client.get(
            "/api/forms/test_simple/submissions/current-steps",
        )
        assert r.status_code == 200, r.text
        opts = r.json()
        # Counts reflect non-deleted set.
        total_count = sum(o["count"] for o in opts)
        assert total_count == 1

    def test_show_deleted_admin_includes_them(
        self, app, admin_client: TestClient,
    ):
        """Admin + `show_deleted=1` — tombstoned rows count toward
        the per-step totals. Used by the dropdown when the
        show-deleted toggle is on, so the user can filter to "step
        X among the deleted rows too"."""
        _start(admin_client, "live")
        gone = _start(admin_client, "gone")
        soft_delete_submissions_by_handles(
            [gone["handle"]], form_id="test_simple",
        )
        r = admin_client.get(
            "/api/forms/test_simple/submissions/current-steps",
            params=[("show_deleted", "1")],
        )
        assert r.status_code == 200, r.text
        opts = r.json()
        total_count = sum(o["count"] for o in opts)
        assert total_count == 2  # both rows in scope now
