"""Which Superset tier a person gets, and what happens when nobody said.

The dangerous direction here is a person getting a capability nobody
declared for them, so most of these assert refusal.
"""

from __future__ import annotations

import types

import pytest

from frontflow.superset import accounts, tiers


@pytest.fixture(autouse=True)
def clean_registry():
    """The registry is module-level and populated at import time."""
    saved = dict(tiers.TIERS)
    tiers.TIERS.clear()
    yield
    tiers.TIERS.clear()
    tiers.TIERS.update(saved)


def _user(username="ada"):
    return types.SimpleNamespace(username=username, email=None)


class TestDeclaration:
    def test_a_group_maps_to_a_tier(self):
        tiers.tier("analysts", tiers.ANALYST)
        assert tiers.role_for_groups(["analysts"]) == tiers.ANALYST

    def test_an_unknown_tier_is_refused_at_declaration(self):
        """A typo must not become a person who is silently never
        provisioned. Superset defines the roles; frontflow cannot
        invent one that carries permissions."""
        with pytest.raises(ValueError, match="Unknown Superset tier"):
            tiers.tier("analysts", "FrontFlow Wizard")

    def test_a_group_cannot_be_declared_twice_differently(self):
        tiers.tier("analysts", tiers.ANALYST)
        with pytest.raises(ValueError, match="already declared"):
            tiers.tier("analysts", tiers.EXPLORER)

    def test_redeclaring_the_same_tier_is_allowed(self):
        """Re-importing a module must not be an error."""
        tiers.tier("analysts", tiers.ANALYST)
        tiers.tier("analysts", tiers.ANALYST)

    def test_an_empty_group_name_is_refused(self):
        with pytest.raises(ValueError, match="needs a group name"):
            tiers.tier("  ", tiers.ANALYST)


class TestResolution:
    def test_highest_tier_wins(self):
        """Matches how folder grants resolve — auth._higher_role."""
        tiers.tier("viewers", tiers.EXPLORER)
        tiers.tier("analysts", tiers.ANALYST)
        assert tiers.role_for_groups(["viewers", "analysts"]) == tiers.ANALYST
        assert tiers.role_for_groups(["analysts", "viewers"]) == tiers.ANALYST

    def test_order_does_not_decide(self):
        """The same set of groups must give the same answer whichever
        way round it arrives."""
        tiers.tier("a", tiers.ANALYST)
        tiers.tier("b", tiers.EXPLORER)
        assert tiers.role_for_groups(["a", "b"]) == tiers.role_for_groups(
            ["b", "a"]
        )

    def test_the_star_group_is_a_default(self):
        tiers.tier("*", tiers.EXPLORER)
        assert tiers.role_for_groups(["anything"]) == tiers.EXPLORER

    def test_a_specific_tier_beats_the_default(self):
        tiers.tier("*", tiers.EXPLORER)
        tiers.tier("analysts", tiers.ANALYST)
        assert tiers.role_for_groups(["analysts"]) == tiers.ANALYST

    def test_no_declaration_means_no_tier(self):
        """Silence is 'not permitted', never 'permitted by default'."""
        assert tiers.role_for_groups(["analysts"]) is None
        assert tiers.role_for_groups([]) is None

    def test_is_configured_distinguishes_off_from_refused(self):
        assert tiers.is_configured() is False
        tiers.tier("*", tiers.EXPLORER)
        assert tiers.is_configured() is True


class TestAccountRefusal:
    """No Superset needed: these all refuse before reaching it."""

    def test_anonymous_is_refused(self):
        tiers.tier("*", tiers.EXPLORER)
        with pytest.raises(accounts.AccountRefused, match="Not signed in"):
            accounts.ensure_account(None, dashboards=[])

    def test_a_user_with_no_tier_is_refused(self, monkeypatch):
        tiers.tier("analysts", tiers.ANALYST)
        monkeypatch.setattr(
            "frontflow.dsl.auth.user_group_names", lambda u: ["sales"]
        )
        with pytest.raises(accounts.AccountRefused, match="not in a group"):
            accounts.ensure_account(_user(), dashboards=[])

    def test_an_install_without_tiers_says_so(self, monkeypatch):
        """A different message from 'you are not permitted' — the
        operator needs to know which of the two it is."""
        monkeypatch.setattr(
            "frontflow.dsl.auth.user_group_names", lambda u: ["sales"]
        )
        with pytest.raises(
            accounts.AccountRefused, match="does not offer Explore"
        ):
            accounts.ensure_account(_user(), dashboards=[])

    def test_a_failing_group_lookup_refuses(self, monkeypatch):
        """A lookup that blows up must not become a granted tier."""
        tiers.tier("*", tiers.EXPLORER)

        def boom(user):
            raise RuntimeError("database on fire")

        monkeypatch.setattr("frontflow.dsl.auth.user_group_names", boom)
        assert tiers.role_for(_user()) is None
        with pytest.raises(accounts.AccountRefused):
            accounts.ensure_account(_user(), dashboards=[])


class TestGroupLookupIsReal:
    """`role_for` reaches the database, and the tests above stub it.

    That stub hid a NameError in `auth.user_group_names` for a full
    build-and-verify cycle: every caller failed closed and refused
    everyone, which looks exactly like a correctly-denied user. Fail-safe
    is not the same as correct, so this exercises the real query.
    """

    def test_membership_round_trips(self, admin_client):
        from frontflow.dsl import auth

        user = auth.create_user("tier_probe", "pw-tier-probe", is_admin=False)
        group = auth.create_group("analysts")
        auth.add_member(group["id"], user.id)

        fresh = auth.get_user_by_username("tier_probe")
        assert auth.user_group_names(fresh) == ["analysts"]

    def test_a_user_in_no_group_gets_an_empty_list(self, admin_client):
        """Not an exception — the default tier depends on this."""
        from frontflow.dsl import auth

        auth.create_user("tier_loner", "pw-tier-loner", is_admin=False)
        fresh = auth.get_user_by_username("tier_loner")
        assert auth.user_group_names(fresh) == []

    def test_role_for_resolves_through_the_real_lookup(self, admin_client):
        from frontflow.dsl import auth

        user = auth.create_user("tier_real", "pw-tier-real", is_admin=False)
        group = auth.create_group("analysts")
        auth.add_member(group["id"], user.id)

        tiers.tier("analysts", tiers.ANALYST)
        tiers.tier("*", tiers.EXPLORER)

        fresh = auth.get_user_by_username("tier_real")
        assert tiers.role_for(fresh) == tiers.ANALYST

    def test_the_default_tier_reaches_an_ungrouped_user(self, admin_client):
        """The case that was silently broken."""
        from frontflow.dsl import auth

        auth.create_user("tier_plain", "pw-tier-plain", is_admin=False)
        tiers.tier("*", tiers.EXPLORER)

        fresh = auth.get_user_by_username("tier_plain")
        assert tiers.role_for(fresh) == tiers.EXPLORER
