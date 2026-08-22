"""Tests for `closes=False` — a form that filters, not one that submits.

The distinction this exists to make: **submitting a control panel runs
its chain without ending it.** A form that pushes data and triggers a
flow should close. A form whose job is to filter a dashboard would be
destroyed by closing, because closing is what stops you filtering again.

`TestStaysOpen` carries the claim. Its non-vacuity rests on `panel`
having a node after it in the fixture, so "nothing downstream appeared"
means something, and on a second submit being an outright 400 on an
ordinary node.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from frontflow.dsl import runtime


FORM = "test_filter_panel"


def _tasks(body: dict) -> list[tuple[str, str]]:
    return [(t["task_id"], t["state"]) for t in body.get("tasks", [])]


def _node_tasks(body: dict) -> list[tuple[str, str]]:
    """Only the form steps — a node's own operators are tasks too."""
    return [
        (t["task_id"], t["state"])
        for t in body.get("tasks", [])
        if t["kind"] == "hitl"
    ]


def _start(client: TestClient, region: str = "north") -> str:
    r = client.post(
        f"/api/forms/{FORM}/submissions", json={"values": {"region": region}}
    )
    assert r.status_code == 201, r.text
    return r.json()["handle"]


def _submit(client: TestClient, handle: str, node: str, values: dict):
    return client.post(
        f"/api/forms/{FORM}/submissions/{handle}/steps/{node}",
        json={"values": values},
    )


class TestDeclaration:
    def test_nodes_close_by_default(self):
        """Opt-in matters: every form written before this depends on the
        old behaviour."""
        from frontflow.dsl.core import Node

        assert Node("n").closes is True

    def test_a_button_defaults_to_following_its_node(self):
        from frontflow import Button

        assert Button("Go").advances is None


class TestStaysOpen:
    """The point of the design."""

    def test_the_node_does_not_close_when_submitted(
        self, admin_client: TestClient
    ):
        handle = _start(admin_client)
        step = runtime.get_submission(handle).steps[0]
        assert step.node_id == "panel"
        assert step.submitted_at is None, "the panel closed"
        assert step.form_values == {"region": "north"}, "values were not kept"

    def test_nothing_downstream_appears(self, admin_client: TestClient):
        """Non-vacuous: `mixed` follows `panel` in the fixture, so on a
        closing node it would be here."""
        handle = _start(admin_client)
        body = admin_client.get(f"/api/forms/{FORM}/submissions/{handle}").json()
        assert _node_tasks(body) == [("panel", "deferred")], _tasks(body)

    def test_it_can_be_submitted_again(self, admin_client: TestClient):
        """The whole point. On a closing node the second call is a 400,
        "Step 'panel' is already submitted"."""
        handle = _start(admin_client)
        again = _submit(admin_client, handle, "panel", {"region": "south"})
        assert again.status_code == 200, again.text

        body = admin_client.get(f"/api/forms/{FORM}/submissions/{handle}").json()
        assert _node_tasks(body) == [("panel", "deferred")]

    def test_each_submit_re_runs_the_chain(self, admin_client: TestClient):
        """A panel that kept the first result would be useless — the
        backend has to see the new values."""
        handle = _start(admin_client)
        assert runtime.get_submission(handle).steps[0].backend_return == {
            "ids": ["north-1"]
        }

        _submit(admin_client, handle, "panel", {"region": "south"})
        assert runtime.get_submission(handle).steps[0].backend_return == {
            "ids": ["south-1"]
        }

    def test_the_panel_keeps_its_latest_values(self, admin_client: TestClient):
        """A control surface should show what it was last set to rather
        than resetting itself."""
        handle = _start(admin_client)
        body = _submit(admin_client, handle, "panel", {"region": "east"}).json()
        assert body["draft"]["values"] == {"region": "east"}

    def test_the_chain_runs_and_its_operators_are_visible(
        self, admin_client: TestClient
    ):
        """The reason a panel exists. `advance()` is what normally ticks
        a node's operators and it only ticks SUBMITTED steps, so without
        deliberate handling a panel would run its backends and drive
        nothing — and even having run, its operators would be hidden
        from the client that has to act on them."""
        handle = _start(admin_client)
        body = admin_client.get(f"/api/forms/{FORM}/submissions/{handle}").json()

        directives = [
            t for t in body["tasks"] if t.get("dashboard_filters")
        ]
        assert directives, _tasks(body)
        assert directives[0]["dashboard_filters"]["dashboard"] == "sales_overview"

    def test_each_apply_raises_a_fresh_directive(
        self, admin_client: TestClient
    ):
        """Tokens must advance, or the browser treats the second apply
        as one it has already handled and the dashboard never moves."""
        handle = _start(admin_client)
        first = admin_client.get(
            f"/api/forms/{FORM}/submissions/{handle}"
        ).json()
        token_a = next(
            t["dashboard_filters"]["token"]
            for t in first["tasks"]
            if t.get("dashboard_filters")
        )

        _submit(admin_client, handle, "panel", {"region": "south"})
        second = admin_client.get(
            f"/api/forms/{FORM}/submissions/{handle}"
        ).json()
        token_b = next(
            t["dashboard_filters"]["token"]
            for t in second["tasks"]
            if t.get("dashboard_filters")
        )
        assert token_b > token_a

    def test_the_submission_is_still_findable(self, admin_client: TestClient):
        """Staying open must not mean never being registered — an
        unfindable submission is not an open one."""
        handle = _start(admin_client)
        assert admin_client.get(
            f"/api/forms/{FORM}/submissions/{handle}"
        ).status_code == 200


class TestClosingStillWorks:
    def test_an_ordinary_node_closes_and_advances(
        self, admin_client: TestClient
    ):
        """The regression guard: `closes` defaults True, so an ordinary
        form must behave exactly as before."""
        r = admin_client.post(
            "/api/forms/test_two_step/submissions",
            json={"values": {"name": "Ada"}},
        )
        assert r.status_code == 201, r.text
        handle = r.json()["handle"]
        step = runtime.get_submission(handle).steps[0]
        assert step.submitted_at is not None
        assert len(runtime.get_submission(handle).steps) > 1, "did not advance"


class TestButtonOverride:
    """`_submit_closes_node` decides, from the node and the button that
    was clicked. Built directly here rather than through a form, so the
    rule is tested on its own terms."""

    @staticmethod
    def _node(closes: bool, **buttons: object):
        from frontflow.dsl.compile import CompiledBlock, CompiledButton, CompiledNode

        return CompiledNode(
            id="n",
            title="N",
            layout=CompiledBlock(type="column"),
            fields=[],
            buttons=[
                CompiledButton(id=bid, label=bid, advances=adv)  # type: ignore[arg-type]
                for bid, adv in buttons.items()
            ],
            closes=closes,
        )

    def test_a_button_without_an_opinion_follows_its_node(self):
        assert runtime._submit_closes_node(
            self._node(False, apply=None), "apply"
        ) is False
        assert runtime._submit_closes_node(
            self._node(True, finish=None), "finish"
        ) is True

    def test_a_button_can_close_a_node_that_would_not(self):
        """What lets a panel apply repeatedly and still offer a way on."""
        node = self._node(False, apply=None, go=True)
        assert runtime._submit_closes_node(node, "apply") is False
        assert runtime._submit_closes_node(node, "go") is True

    def test_a_button_can_keep_open_a_node_that_would_close(self):
        node = self._node(True, save=None, preview=False)
        assert runtime._submit_closes_node(node, "save") is True
        assert runtime._submit_closes_node(node, "preview") is False

    def test_an_unknown_button_falls_back_to_the_node(self):
        """Defensive: an id that matches nothing must not read as
        "advances", which would close a panel by accident."""
        assert runtime._submit_closes_node(
            self._node(False, apply=None), "nope"
        ) is False
