"""Tests for `superset.RefreshDashboard`.

The claim this operator exists to make: **where the author places it in
the `>>` chain is when the refresh happens.** A refresh that fired
implicitly on every submit would need none of this machinery — and would
be useless for the case that motivates it, where the dashboard should
only move once a pipeline has actually finished loading data.

So the load-bearing test here is not "does a refresh happen" but "does
placement change *when* it happens" (`TestPlacementControlsTiming`).
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from frontflow.superset import operators


FORM = "test_refresh_dashboard"


def _tasks(body: dict) -> list[dict]:
    return body.get("tasks", [])


def _refresh_tasks(body: dict) -> list[dict]:
    return [t for t in _tasks(body) if t.get("dashboard_refresh")]


class TestTimeRange:
    def test_left_side_is_empty_not_no_filter(self):
        """Superset splits on " : " and treats an empty side as
        unbounded. The literal "No filter" there is a parse error —
        `Cannot parse time string [No filter]` — so this is asserted
        rather than left to a comment."""
        value = operators.next_time_range()
        assert value.startswith(" : ")
        assert "No filter" not in value

    def test_format_has_no_fraction_or_timezone_suffix(self):
        """Superset's own format. Fractional seconds and a trailing Z
        are both riskier to parse."""
        value = operators.next_time_range()
        assert re.fullmatch(r" : \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", value)

    def test_value_strictly_advances(self):
        """The whole mechanism is that the value moves the query's cache
        key. At second resolution two refreshes in the same second would
        otherwise be identical — and nothing would visibly update."""
        values = [operators.next_time_range() for _ in range(5)]
        assert len(set(values)) == 5
        assert values == sorted(values)

    def test_upper_bound_is_in_the_future(self):
        """The bound moves the cache key; it must not exclude a row
        written moments ago because two clocks disagree."""
        import datetime as dt

        stamp = operators.next_time_range().removeprefix(" : ")
        parsed = dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=dt.timezone.utc
        )
        assert parsed > dt.datetime.now(dt.timezone.utc)


class TestOperator:
    def test_requires_a_name(self):
        with pytest.raises(ValueError):
            operators.RefreshDashboard("")

    def test_is_graph_visible(self):
        """An authored step, not plumbing — the chain UI should show
        where the refresh happens."""
        assert operators.RefreshDashboard("d").graph_visible is True


class TestPlacementControlsTiming:
    """The point of the design."""

    def test_refresh_with_no_upstream_operator_fires_immediately(
        self, admin_client: TestClient
    ):
        """`early` wires submit >> RefreshDashboard directly."""
        r = admin_client.post(
            f"/api/forms/{FORM}/submissions", json={"values": {"region": "North"}}
        )
        assert r.status_code == 201, r.text

        refreshes = _refresh_tasks(r.json())
        assert len(refreshes) == 1, _tasks(r.json())
        assert refreshes[0]["state"] == "success"
        assert refreshes[0]["dashboard_refresh"]["dashboard"] == "sales_overview"

    def test_refresh_behind_a_sensor_does_not_fire_until_it_succeeds(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        """`gated` wires submit >> mock sensor >> RefreshDashboard.

        Two halves, and both are needed. First: while the sensor is
        still queued, the refresh behind it must not have run — an
        implicit on-submit refresh would already have fired. Second:
        once the sensor succeeds, the refresh *does* run. Same
        placement, different sensor state, different outcome — which is
        the whole claim.
        """
        created = admin_client.post(
            f"/api/forms/{FORM}/submissions", json={"values": {"region": "North"}}
        )
        sid = created.json()["submission_id"]

        advanced = admin_client.post(
            f"/api/forms/{FORM}/submissions/{sid}/steps/gated", json={"values": {}}
        )
        assert advanced.status_code in (200, 201), advanced.text

        body = admin_client.get(f"/api/forms/{FORM}/submissions/{sid}").json()

        # Non-vacuous: the sensor must actually be present and unfinished,
        # or "no refresh ran" would prove nothing.
        sensors = [t for t in _tasks(body) if t["task_id"] == "load"]
        assert sensors, f"mock sensor row missing: {_tasks(body)}"
        assert sensors[0]["state"] != "success", (
            "sensor already succeeded; this test cannot distinguish "
            "placement from an implicit refresh"
        )

        gated_refresh = [
            t for t in _refresh_tasks(body) if t["task_id"] == "refresh_gated"
        ]
        assert not gated_refresh, (
            "a refresh placed after a sensor fired before the sensor "
            "succeeded — placement is not controlling timing"
        )

        # The `early` node's refresh HAS legitimately run, which confirms
        # refreshes fire at all and the absence above is about placement.
        assert any(
            t["task_id"] == "refresh_early" for t in _refresh_tasks(body)
        )

        # --- now let the sensor finish -------------------------------
        # The mock sensor's state is a pure function of elapsed time;
        # collapsing the timings is faster and steadier than sleeping.
        import frontflow.dsl.runtime as runtime_mod

        monkeypatch.setattr(runtime_mod, "QUEUED_INITIAL", 0.0)
        monkeypatch.setattr(runtime_mod, "RUN_DURATION", 0.0)

        settled = admin_client.get(
            f"/api/forms/{FORM}/submissions/{sid}"
        ).json()

        sensors = [t for t in _tasks(settled) if t["task_id"] == "load"]
        assert sensors and sensors[0]["state"] == "success", (
            f"sensor did not settle: {_tasks(settled)}"
        )

        gated_refresh = [
            t for t in _refresh_tasks(settled) if t["task_id"] == "refresh_gated"
        ]
        assert gated_refresh, (
            "the sensor succeeded but the refresh behind it never ran"
        )
        assert (
            gated_refresh[0]["dashboard_refresh"]["dashboard"] == "sales_overview"
        )

    def test_refresh_is_fire_and_forget(self, admin_client: TestClient):
        """It must never block the chain waiting for a browser: a
        submission has to progress with no client attached."""
        r = admin_client.post(
            f"/api/forms/{FORM}/submissions", json={"values": {"region": "North"}}
        )
        refresh = _refresh_tasks(r.json())[0]
        assert refresh["state"] == "success"


class TestDirectiveDelivery:
    def test_directive_reaches_the_polled_payload(
        self, admin_client: TestClient
    ):
        """No new transport: the client already polls this endpoint, so
        the directive rides state it is fetching anyway."""
        created = admin_client.post(
            f"/api/forms/{FORM}/submissions", json={"values": {"region": "North"}}
        )
        sid = created.json()["submission_id"]

        body = admin_client.get(f"/api/forms/{FORM}/submissions/{sid}").json()
        directives = [t["dashboard_refresh"] for t in _refresh_tasks(body)]

        assert directives, "no refresh directive in the polled submission"
        assert set(directives[0]) == {"dashboard", "time_range", "token"}

    def test_token_is_stable_across_polls(self, admin_client: TestClient):
        """Re-polling the same chain state must not look like a new
        refresh, or an open dashboard would re-query on every poll."""
        created = admin_client.post(
            f"/api/forms/{FORM}/submissions", json={"values": {"region": "North"}}
        )
        sid = created.json()["submission_id"]

        first = admin_client.get(f"/api/forms/{FORM}/submissions/{sid}").json()
        second = admin_client.get(f"/api/forms/{FORM}/submissions/{sid}").json()

        assert (
            _refresh_tasks(first)[0]["dashboard_refresh"]["token"]
            == _refresh_tasks(second)[0]["dashboard_refresh"]["token"]
        )
