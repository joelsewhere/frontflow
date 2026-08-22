"""Tests for `superset.SetFilters`.

The claim this operator exists to make: **what a dashboard is filtered
to can be decided by a `@backend`, at the point in the chain the author
put the operator.** A filter value is an ordinary template resolved
against prior steps, the same way `AirflowStatus.run_id` is.

So the load-bearing test is `TestBackendDrivesTheFilters` — a value the
form never collected, produced by a backend, arriving in the directive.
Everything else guards the edges around it.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from frontflow.superset import operators


FORM = "test_dashboard_filters"


def _tasks(body: dict) -> list[dict]:
    return body.get("tasks", [])


def _filter_tasks(body: dict) -> list[dict]:
    return [t for t in _tasks(body) if t.get("dashboard_filters")]


class TestOperator:
    def test_requires_a_dashboard_name(self):
        with pytest.raises(ValueError):
            operators.SetFilters("")

    def test_requires_at_least_one_filter(self):
        """Setting no filters is never what the author meant, and would
        otherwise succeed silently."""
        with pytest.raises(ValueError, match="sets no filters"):
            operators.SetFilters("sales_overview")

    def test_is_graph_visible(self):
        """An authored step: the chain UI should show where the
        dashboard gets pointed somewhere new."""
        assert operators.SetFilters("d", region="East").graph_visible is True

    def test_tokens_strictly_advance(self):
        """A block applies a directive once per token it has not seen.
        Two directives inside the same second must still be
        distinguishable, or the second is silently dropped."""
        first = operators.build_filter_directive("d", {"a": "1"})
        second = operators.build_filter_directive("d", {"a": "2"})
        assert second["token"] > first["token"]


class TestBackendDrivesTheFilters:
    """The point of the design."""

    def test_a_backend_return_reaches_the_directive(
        self, admin_client: TestClient
    ):
        """`segment` is produced by the backend and never collected by
        the form, so its presence cannot be explained by the value
        having been typed in."""
        r = admin_client.post(
            f"/api/forms/{FORM}/submissions",
            json={"values": {"region": "north"}},
        )
        assert r.status_code == 201, r.text

        directives = _filter_tasks(r.json())
        assert len(directives) == 1, _tasks(r.json())

        payload = directives[0]["dashboard_filters"]
        assert payload["dashboard"] == "sales_overview"
        assert payload["filters"]["Segment"] == "enterprise"

    def test_the_backend_can_transform_a_submitted_value(
        self, admin_client: TestClient
    ):
        """"north" was submitted; the backend title-cases it. The
        directive must carry the backend's version, not the raw input —
        otherwise the template is resolving against form values and the
        backend is doing nothing."""
        r = admin_client.post(
            f"/api/forms/{FORM}/submissions",
            json={"values": {"region": "north"}},
        )
        payload = _filter_tasks(r.json())[0]["dashboard_filters"]
        assert payload["filters"]["Region"] == "North"

    def test_a_literal_value_needs_no_template(
        self, admin_client: TestClient
    ):
        created = admin_client.post(
            f"/api/forms/{FORM}/submissions",
            json={"values": {"region": "north"}},
        )
        sid = created.json()["submission_id"]

        advanced = admin_client.post(
            f"/api/forms/{FORM}/submissions/{sid}/steps/literal",
            json={"values": {}},
        )
        assert advanced.status_code in (200, 201), advanced.text

        body = admin_client.get(f"/api/forms/{FORM}/submissions/{sid}").json()
        pinned = [
            t for t in _filter_tasks(body) if t["task_id"] == "pin_east"
        ]
        assert pinned, _tasks(body)
        assert pinned[0]["dashboard_filters"]["filters"] == {"Region": "East"}


class TestFireAndForget:
    def test_it_succeeds_without_any_client_attached(
        self, admin_client: TestClient
    ):
        """The chain must progress with nobody watching. A dashboard
        that is not open simply never sees the directive."""
        r = admin_client.post(
            f"/api/forms/{FORM}/submissions",
            json={"values": {"region": "north"}},
        )
        assert _filter_tasks(r.json())[0]["state"] == "success"

    def test_the_detail_names_the_filters_it_set(
        self, admin_client: TestClient
    ):
        """What happened has to be legible from the chain UI, since the
        effect itself is invisible unless a dashboard is open."""
        r = admin_client.post(
            f"/api/forms/{FORM}/submissions",
            json={"values": {"region": "north"}},
        )
        detail = _filter_tasks(r.json())[0]["detail"]
        assert "sales_overview" in detail
        assert "Region" in detail
