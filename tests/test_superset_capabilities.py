"""The per-user capability callable in deploy/superset/superset_config.py.

This is deployment config rather than library code, so it is loaded here
by executing the file with a stubbed environment. That is worth the
awkwardness: the dangerous regression is a capability FAILING OPEN, and
the direction of an incorrect answer is invisible in a config file that
nothing imports.

The role→capability decision is Superset-side because Superset is where
it is enforced, and it is keyed on role because roles are what frontflow
provisions per user.
"""

from __future__ import annotations

import os
import pathlib
import sys
import types

import pytest

CONFIG = (
    pathlib.Path(__file__).resolve().parents[1]
    / "deploy"
    / "superset"
    / "superset_config.py"
)


class _FakeUser:
    def __init__(self, *role_names):
        self.roles = [types.SimpleNamespace(name=n) for n in role_names]
        self.is_authenticated = True


class _StubEnv(dict):
    """Any variable the config asks for exists and is a placeholder.

    Enumerating the real list would make this test fail whenever the
    deployment grows a setting, which tells us nothing about the thing
    under test.
    """

    def __missing__(self, key):
        return "stub"


def _load_config(monkeypatch, current_user):
    """Exec the Superset config with flask_login and os.environ stubbed."""
    monkeypatch.setattr(os, "environ", _StubEnv())

    fake_flask_login = types.ModuleType("flask_login")
    fake_flask_login.current_user = current_user
    monkeypatch.setitem(sys.modules, "flask_login", fake_flask_login)

    namespace: dict = {"__name__": "superset_config", "__file__": str(CONFIG)}
    exec(compile(CONFIG.read_text(), str(CONFIG), "exec"), namespace)  # noqa: S102
    return namespace


def _resolve(monkeypatch, current_user, feature, default):
    ns = _load_config(monkeypatch, current_user)
    return ns["IS_FEATURE_ENABLED_FUNC"](feature, default)


GOVERNED = "ALLOW_ADHOC_SUBQUERY"


def test_listed_capability_granted_to_named_role(monkeypatch):
    assert _resolve(monkeypatch, _FakeUser("FrontFlow Analyst"), GOVERNED, False) is True


def test_listed_capability_withheld_from_other_roles(monkeypatch):
    assert (
        _resolve(monkeypatch, _FakeUser("FrontFlow Explorer"), GOVERNED, False) is False
    )


def test_listed_capability_withheld_even_when_globally_enabled(monkeypatch):
    """The load-bearing one.

    An operator turning the flag on install-wide must not widen it to
    everyone. Without this the "withheld" test above passes for the wrong
    reason — the global default is already False.
    """
    assert (
        _resolve(monkeypatch, _FakeUser("FrontFlow Explorer"), GOVERNED, True) is False
    )


def test_guest_and_anonymous_callers_fail_closed(monkeypatch):
    """Guest-token dashboard viewers carry the stock Gamma role.

    They are `is_authenticated`, so a check that keys on authentication
    rather than on roles hands them the global default — which is
    precisely backwards for the least-trusted caller on the system.
    """
    assert _resolve(monkeypatch, _FakeUser("Gamma"), GOVERNED, True) is False
    assert _resolve(monkeypatch, None, GOVERNED, True) is False
    assert _resolve(monkeypatch, _FakeUser(), GOVERNED, True) is False


@pytest.mark.parametrize("default", [True, False])
def test_unlisted_flags_pass_through_untouched(monkeypatch, default):
    """This only ever narrows: a flag nobody listed keeps its default."""
    user = _FakeUser("FrontFlow Explorer")
    assert _resolve(monkeypatch, user, "CSS_TEMPLATES", default) is default


def test_rls_in_sqllab_is_global_not_per_role(monkeypatch):
    """RLS_IN_SQLLAB is a protection, not a capability.

    If it ever appears in PER_ROLE_FEATURES, some role can be handed SQL
    Lab with RLS silently off for them.
    """
    ns = _load_config(monkeypatch, _FakeUser("Admin"))
    assert "RLS_IN_SQLLAB" not in ns["PER_ROLE_FEATURES"]
    assert ns["FEATURE_FLAGS"]["RLS_IN_SQLLAB"] is True
