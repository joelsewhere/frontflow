"""Saving a workspace arrangement.

Three tiers decide what someone sees: this user's layout for this width
band, the workspace's layout for that band, and the arrangement the DSL
declares. The DSL keeps deciding which panels EXIST; only the
arrangement is overridable.

The direction that matters is a person writing the layout everyone gets
without the right to. `TestAuthoring` is the load-bearing part.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient



# Declared in tests/fixtures/forms/test_workspace.py so the scanner
# creates its visibility row too — a workspace registered by hand has no
# database row, and every route here is behind the visibility gate.
WS = "ops_bands"
GRID = {"grid": {"root": {"type": "leaf", "data": {"views": ["a"]}}}}


def login(client: TestClient, who: dict[str, str]) -> None:
    """Switch the client's identity.

    `admin_client` and `user_client` are the SAME TestClient with the
    same cookie jar — whichever fixture ran last is who you are. Any
    test needing two people has to switch deliberately rather than
    request both fixtures and assume they differ.
    """
    r = client.post("/api/auth/login", json=who)
    assert r.status_code == 200, r.text


class TestBands:
    def test_the_declared_bands_include_the_implicit_base(
        self, admin_client: TestClient
    ):
        r = admin_client.get(f"/api/workspaces/{WS}/layout")
        assert r.status_code == 200, r.text
        assert r.json()["bands"] == [0, 900, 1400]

    def test_an_undeclared_band_is_refused(self, admin_client: TestClient):
        """Storing an arbitrary width makes rows nothing reads again the
        moment the author edits their breakpoints."""
        r = admin_client.get(f"/api/workspaces/{WS}/layout?band=1234")
        assert r.status_code == 400
        assert "no band starting at 1234" in r.json()["detail"]

    def test_saving_to_an_undeclared_band_is_refused(
        self, admin_client: TestClient
    ):
        r = admin_client.put(
            f"/api/workspaces/{WS}/layout",
            json={"layout": GRID, "band": 1234},
        )
        assert r.status_code == 400

    def test_each_band_is_stored_separately(self, admin_client: TestClient):
        for band in (0, 900, 1400):
            admin_client.put(
                f"/api/workspaces/{WS}/layout",
                json={"layout": {"band": band}, "band": band},
            )
        for band in (0, 900, 1400):
            got = admin_client.get(
                f"/api/workspaces/{WS}/layout?band={band}"
            ).json()
            assert got["user"] == {"band": band}


class TestTiers:
    def test_nothing_saved_returns_both_tiers_empty(
        self, admin_client: TestClient
    ):
        got = admin_client.get(f"/api/workspaces/{WS}/layout").json()
        assert got["user"] is None and got["workspace"] is None

    def test_the_two_tiers_are_returned_separately(
        self, admin_client: TestClient
    ):
        """Not resolved into one answer: the UI has to say WHICH it is
        showing, and only a personal layout offers a reset."""
        admin_client.put(
            f"/api/workspaces/{WS}/layout",
            json={"layout": {"who": "everyone"}, "for_everyone": True},
        )
        admin_client.put(
            f"/api/workspaces/{WS}/layout", json={"layout": {"who": "me"}}
        )
        got = admin_client.get(f"/api/workspaces/{WS}/layout").json()
        assert got["workspace"] == {"who": "everyone"}
        assert got["user"] == {"who": "me"}

    def test_one_users_layout_is_not_anothers(
        self, anon_client: TestClient, admin_user, regular_user
    ):
        login(anon_client, admin_user)
        anon_client.put(
            f"/api/workspaces/{WS}/layout", json={"layout": {"who": "admin"}}
        )
        login(anon_client, regular_user)
        got = anon_client.get(f"/api/workspaces/{WS}/layout").json()
        assert got["user"] is None

    def test_the_author_layout_is_visible_to_everyone(
        self, anon_client: TestClient, admin_user, regular_user
    ):
        login(anon_client, admin_user)
        anon_client.put(
            f"/api/workspaces/{WS}/layout",
            json={"layout": {"who": "everyone"}, "for_everyone": True},
        )
        login(anon_client, regular_user)
        got = anon_client.get(f"/api/workspaces/{WS}/layout").json()
        assert got["workspace"] == {"who": "everyone"}


class TestAuthoring:
    """Writing the layout everyone gets requires 'manage'."""

    def test_a_plain_user_cannot_write_for_everyone(
        self, user_client: TestClient
    ):
        r = user_client.put(
            f"/api/workspaces/{WS}/layout",
            json={"layout": GRID, "for_everyone": True},
        )
        assert r.status_code == 403

    def test_a_refused_write_stores_nothing(
        self, anon_client: TestClient, admin_user, regular_user
    ):
        """The over-reach check: a 403 must not have written first."""
        login(anon_client, regular_user)
        anon_client.put(
            f"/api/workspaces/{WS}/layout",
            json={"layout": {"who": "sneak"}, "for_everyone": True},
        )
        login(anon_client, admin_user)
        assert anon_client.get(f"/api/workspaces/{WS}/layout").json()[
            "workspace"
        ] is None

    def test_a_plain_user_can_still_save_their_own(
        self, user_client: TestClient
    ):
        r = user_client.put(
            f"/api/workspaces/{WS}/layout", json={"layout": {"who": "me"}}
        )
        assert r.status_code == 200, r.text
        assert user_client.get(f"/api/workspaces/{WS}/layout").json()[
            "user"
        ] == {"who": "me"}

    def test_can_author_reports_the_right_answer(
        self, anon_client: TestClient, admin_user, regular_user
    ):
        login(anon_client, admin_user)
        assert anon_client.get(f"/api/workspaces/{WS}/layout").json()[
            "can_author"
        ] is True
        login(anon_client, regular_user)
        assert anon_client.get(f"/api/workspaces/{WS}/layout").json()[
            "can_author"
        ] is False

    def test_an_anonymous_visitor_is_refused_rather_than_ignored(
        self, anon_client: TestClient
    ):
        r = anon_client.put(
            f"/api/workspaces/{WS}/layout", json={"layout": GRID}
        )
        assert r.status_code in (401, 403)


class TestReset:
    def test_resetting_drops_only_this_users_layout(
        self, admin_client: TestClient
    ):
        admin_client.put(
            f"/api/workspaces/{WS}/layout",
            json={"layout": {"who": "everyone"}, "for_everyone": True},
        )
        admin_client.put(
            f"/api/workspaces/{WS}/layout", json={"layout": {"who": "me"}}
        )

        admin_client.delete(f"/api/workspaces/{WS}/layout")

        got = admin_client.get(f"/api/workspaces/{WS}/layout").json()
        assert got["user"] is None
        assert got["workspace"] == {"who": "everyone"}, "author layout kept"

    def test_resetting_clears_every_band_by_default(
        self, admin_client: TestClient
    ):
        for band in (0, 900, 1400):
            admin_client.put(
                f"/api/workspaces/{WS}/layout",
                json={"layout": {"b": band}, "band": band},
            )
        admin_client.delete(f"/api/workspaces/{WS}/layout")
        for band in (0, 900, 1400):
            assert admin_client.get(
                f"/api/workspaces/{WS}/layout?band={band}"
            ).json()["user"] is None

    def test_resetting_one_band_leaves_the_others(
        self, admin_client: TestClient
    ):
        for band in (0, 900):
            admin_client.put(
                f"/api/workspaces/{WS}/layout",
                json={"layout": {"b": band}, "band": band},
            )
        admin_client.delete(f"/api/workspaces/{WS}/layout?band=900")
        assert admin_client.get(f"/api/workspaces/{WS}/layout?band=0").json()[
            "user"
        ] == {"b": 0}
        assert admin_client.get(
            f"/api/workspaces/{WS}/layout?band=900"
        ).json()["user"] is None

    def test_a_plain_user_cannot_reset_for_everyone(
        self, anon_client: TestClient, admin_user, regular_user
    ):
        login(anon_client, admin_user)
        anon_client.put(
            f"/api/workspaces/{WS}/layout",
            json={"layout": {"who": "everyone"}, "for_everyone": True},
        )
        login(anon_client, regular_user)
        r = anon_client.delete(f"/api/workspaces/{WS}/layout?for_everyone=true")
        assert r.status_code == 403

        login(anon_client, admin_user)
        assert anon_client.get(f"/api/workspaces/{WS}/layout").json()[
            "workspace"
        ] == {"who": "everyone"}
