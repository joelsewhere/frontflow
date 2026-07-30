"""Tests for the paginated `GET /api/forms/{id}/submissions` listing.

Covers the three new pieces of the form-summary submissions tab:
  - server-side pagination via `limit` and `offset`
  - filtering via `state` (multi-select) and `q` (search)
  - multi-column sort via repeated `sort=column:direction` params

All tests run against the `test_simple` fixture form so the
submission lifecycle stays trivial (single node → terminal). The
listing endpoint is what the UI calls; the store function it
delegates to is exercised through the endpoint, which is the
contract that matters.
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def _start(client: TestClient, name: str, note: str = "ok") -> dict:
    """Create one submission on `test_simple`; return its summary
    row (handle + minted submission_id)."""
    r = client.post(
        "/api/forms/test_simple/submissions",
        json={"values": {"name": name, "note": note}},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _listing(
    client: TestClient,
    *,
    limit: int | None = None,
    offset: int | None = None,
    state: list[str] | None = None,
    q: str | None = None,
    sort: list[str] | None = None,
) -> dict:
    """GET the paginated listing with explicit URL params."""
    params: list[tuple[str, str]] = []
    if limit is not None:
        params.append(("limit", str(limit)))
    if offset is not None:
        params.append(("offset", str(offset)))
    for s in state or ():
        params.append(("state", s))
    if q is not None:
        params.append(("q", q))
    for s in sort or ():
        params.append(("sort", s))
    r = client.get(
        "/api/forms/test_simple/submissions", params=params,
    )
    assert r.status_code == 200, r.text
    return r.json()


# --- Pagination ------------------------------------------------------------


class TestPagination:
    def test_envelope_shape(self, app, admin_client: TestClient):
        """Response is the wrapped page, not a flat list — that's
        the breaking-change the frontend depends on (it reads
        `.submissions` and `.total` directly)."""
        _start(admin_client, "page-1")
        page = _listing(admin_client)
        assert set(page.keys()) >= {
            "submissions", "total", "limit", "offset",
        }
        assert isinstance(page["submissions"], list)

    def test_limit_offset_window(self, app, admin_client: TestClient):
        """With 3 submissions and limit=2, the second page contains
        the remaining 1 row and `total` reflects 3 across both."""
        for i in range(3):
            _start(admin_client, f"page-{i}")
        first = _listing(admin_client, limit=2, offset=0)
        second = _listing(admin_client, limit=2, offset=2)
        assert first["total"] == 3
        assert second["total"] == 3
        assert len(first["submissions"]) == 2
        assert len(second["submissions"]) == 1
        # Pages are disjoint — the handle on page 2 must not appear
        # on page 1. (Without the final handle tiebreaker in the
        # store's ORDER BY, two rows tying on the primary sort
        # column could shuffle between pages on refetch.)
        seen = {s["handle"] for s in first["submissions"]}
        assert second["submissions"][0]["handle"] not in seen

    def test_limit_clamped_to_max(
        self, app, admin_client: TestClient,
    ):
        """A `limit` above the cap (100) is clamped, not 400'd —
        a runaway client should degrade, not break."""
        _start(admin_client, "clamp")
        page = _listing(admin_client, limit=99999)
        assert page["limit"] == 100  # the cap

    def test_offset_past_end_returns_empty_page(
        self, app, admin_client: TestClient,
    ):
        """Offset beyond `total` is not an error — it's a valid
        empty page. The UI uses this to know it's at the end."""
        _start(admin_client, "past-end")
        page = _listing(admin_client, offset=1000)
        assert page["submissions"] == []
        assert page["total"] == 1


# --- Filtering -------------------------------------------------------------


class TestStateFilter:
    def test_multi_select_returns_union(
        self, app, admin_client: TestClient,
    ):
        """A `state=success&state=failed` request returns rows in
        EITHER state — multi-select acts as a set union."""
        _start(admin_client, "state-ok")  # success
        # Force one into a failed state by direct DB write — the
        # test fixture form doesn't have a failure path.
        from sqlalchemy import update as _update
        from frontflow.dsl.store import (
            Session, Submission, _engine,
        )
        bad = _start(admin_client, "state-fail")
        with Session(_engine) as s:
            s.execute(
                _update(Submission)
                .where(Submission.handle == bad["handle"])
                .values(state="failed")
            )
            s.commit()

        page = _listing(admin_client, state=["failed"])
        states = {r["state"] for r in page["submissions"]}
        assert states == {"failed"}

        page = _listing(admin_client, state=["success", "failed"])
        states = {r["state"] for r in page["submissions"]}
        assert states == {"success", "failed"}

    def test_unknown_state_value_is_dropped_not_500(
        self, app, admin_client: TestClient,
    ):
        """A typo'd state in the URL silently drops to "no filter"
        rather than 500-ing the listing. Brittle URLs from old UI
        revisions shouldn't break the page."""
        _start(admin_client, "unknown")
        page = _listing(admin_client, state=["banana"])
        # The unknown filter dropped → no state restriction → the
        # row comes back.
        assert page["total"] == 1


class TestSearchQuery:
    def test_matches_submission_id_substring_case_insensitive(
        self, app, admin_client: TestClient,
    ):
        """The search box is the listing's quickest filter; case-
        insensitive substring match keeps it forgiving."""
        # The `test_simple` fixture doesn't template submission_id —
        # rows come back with NULL there. Force a known value via
        # direct DB write so we can verify the submission_id branch
        # of the OR-search clause (the handle branch is exercised
        # by the next test).
        from sqlalchemy import update as _update
        from frontflow.dsl.store import (
            Session, Submission, _engine,
        )
        target = _start(admin_client, "search-target")
        _start(admin_client, "search-other")
        with Session(_engine) as s:
            s.execute(
                _update(Submission)
                .where(Submission.handle == target["handle"])
                .values(submission_id="AlphaBetSubmissionFoo")
            )
            s.commit()

        page = _listing(admin_client, q="ALPHABET")
        handles = {r["handle"] for r in page["submissions"]}
        assert target["handle"] in handles
        assert page["total"] == 1

    def test_matches_handle_too(
        self, app, admin_client: TestClient,
    ):
        """The search field doubles as a way to look up a row by
        its handle — useful for triage from a log line that pasted
        the handle."""
        row = _start(admin_client, "id-match")
        # Handle is server-generated; search for a suffix of it.
        suffix = row["handle"][-6:]
        page = _listing(admin_client, q=suffix)
        assert any(
            r["handle"] == row["handle"]
            for r in page["submissions"]
        )

    def test_like_metacharacter_does_not_broaden(
        self, app, admin_client: TestClient,
    ):
        """`%` in user input must NOT become a SQL wildcard —
        searching for `%` should return rows whose id literally
        contains `%` (none in this fixture), not "everything"."""
        _start(admin_client, "metachar-test")
        page = _listing(admin_client, q="%")
        assert page["total"] == 0


# --- Sort ------------------------------------------------------------------


class TestSort:
    def test_default_sort_is_created_at_desc(
        self, app, admin_client: TestClient,
    ):
        """Default sort matches the legacy listing's order — newest
        first. Without specifying `sort`, the page comes back in
        descending `created_at`."""
        first = _start(admin_client, "older")
        second = _start(admin_client, "newer")
        page = _listing(admin_client)
        handles = [r["handle"] for r in page["submissions"]]
        # Newest first; the second-created is at index 0.
        assert handles.index(second["handle"]) < handles.index(
            first["handle"]
        )

    def test_explicit_asc_sort_reverses_order(
        self, app, admin_client: TestClient,
    ):
        """Explicit `created_at:asc` puts the oldest at the top."""
        first = _start(admin_client, "first")
        second = _start(admin_client, "second")
        page = _listing(admin_client, sort=["created_at:asc"])
        handles = [r["handle"] for r in page["submissions"]]
        assert handles.index(first["handle"]) < handles.index(
            second["handle"]
        )

    def test_multi_column_sort_applies_in_order(
        self, app, admin_client: TestClient,
    ):
        """Multiple `sort=` params chain as primary, secondary,
        … The primary sort dominates; the secondary breaks ties.
        We construct two rows that tie on `state` and differ on
        `created_at`, then sort by `state:asc, created_at:desc`
        and verify the within-state order matches the secondary."""
        a = _start(admin_client, "tie-a")
        b = _start(admin_client, "tie-b")
        # Both will have state="success" — the secondary sort on
        # created_at:desc puts the newer (b) first within the tie.
        page = _listing(
            admin_client,
            sort=["state:asc", "created_at:desc"],
        )
        handles = [r["handle"] for r in page["submissions"]]
        assert handles.index(b["handle"]) < handles.index(
            a["handle"]
        )

    def test_unknown_sort_column_is_dropped(
        self, app, admin_client: TestClient,
    ):
        """An unknown column key from a stale URL silently falls
        back to the default sort rather than 500ing or returning
        an unordered page."""
        _start(admin_client, "unknown-col-1")
        _start(admin_client, "unknown-col-2")
        # Both sort entries unknown → effective sort is empty →
        # store falls back to created_at:desc.
        page = _listing(
            admin_client, sort=["bogus:asc", "alsobogus:desc"],
        )
        assert page["total"] == 2

    def test_unknown_direction_is_dropped(
        self, app, admin_client: TestClient,
    ):
        """A direction other than asc/desc is dropped (the entry
        is malformed, not a different ordering rule we forgot to
        implement)."""
        _start(admin_client, "dir-1")
        page = _listing(
            admin_client, sort=["created_at:sideways"],
        )
        assert page["total"] == 1  # request didn't 500

    def test_updated_at_column_is_returned_and_sortable(
        self, app, admin_client: TestClient,
    ):
        """`updated_at` is the new "Last activity" column — must
        be present on each row AND sortable. Without this the UI
        can't render the column or its sort header."""
        a = _start(admin_client, "act-a")
        b = _start(admin_client, "act-b")
        page = _listing(admin_client, sort=["updated_at:desc"])
        rows = page["submissions"]
        # Each row carries `updated_at`.
        for r in rows:
            assert "updated_at" in r
        # The row created later updated later → comes first in
        # the desc sort.
        handles = [r["handle"] for r in rows]
        assert handles.index(b["handle"]) < handles.index(
            a["handle"]
        )
