"""Tests for resolving a dashboard *name* into something embeddable.

Workflow authors reference dashboards by name; this layer creates them
in Superset on first use. The behaviour worth protecting is what happens
when Superset is *not* available: a form must still render. Refusing to
serve a form because a BI tool is down would be the wrong trade, so
provisioning persists a partial binding and leaves the rest to `repair`.
"""
from __future__ import annotations

import json

import httpx
import pytest

from frontflow.dsl import store
from frontflow.superset import provisioning as pv
from frontflow.superset.client import SupersetClient

# `tests/` is not a package; pytest puts the test directory on
# sys.path, so this is a plain absolute import.
from test_superset_client import FakeSuperset


@pytest.fixture
def superset(app, monkeypatch: pytest.MonkeyPatch) -> FakeSuperset:
    """A fake Superset, with every SupersetClient routed to it."""
    store.upsert_connection(
        name="superset_default",
        conn_type="superset",
        base_url="http://superset.test:8088",
        auth_kind="basic",
        secret={"username": "admin", "password": "hunter2"},
    )
    SupersetClient._token_cache.clear()

    fake = FakeSuperset()
    original_init = SupersetClient.__init__

    def patched_init(self, connection=None):
        original_init(self, connection)
        self._http = httpx.Client(
            transport=httpx.MockTransport(fake.handler), timeout=5
        )

    monkeypatch.setattr(SupersetClient, "__init__", patched_init)
    return fake


class TestFirstUse:
    def test_unknown_name_provisions_the_whole_chain(self, superset):
        """Dashboard, dataset, refresh filter, and embed config — so a
        name referenced in a workflow works against an empty Superset."""
        binding = pv.resolve_dashboard("sales_overview")

        assert binding["superset_dashboard_id"] is not None
        assert binding["embed_uuid"] is not None
        assert binding["filter_id"] is not None
        assert binding["auto_created"] is True

    def test_the_filter_targets_created_at(self, superset):
        """The refresh operator drives this filter; on any other column
        it would not move the query's cache key."""
        binding = pv.resolve_dashboard("sales_overview")
        metadata = json.loads(
            superset.metadata[binding["superset_dashboard_id"]]
        )
        native = metadata["native_filter_configuration"][0]
        assert native["filterType"] == "filter_time"
        assert native["targets"][0]["column"]["name"] == "created_at"

    def test_second_reference_reuses_the_binding(self, superset):
        """Otherwise every render would create another dashboard."""
        first = pv.resolve_dashboard("sales_overview")
        created_after_first = superset.dashboards_created
        second = pv.resolve_dashboard("sales_overview")

        assert superset.dashboards_created == created_after_first
        assert second["superset_dashboard_id"] == first["superset_dashboard_id"]


class TestPureLookup:
    def test_provision_false_never_creates(self, superset):
        """Build-time validation must not create dashboards as a side
        effect of checking that a name is valid."""
        assert pv.resolve_dashboard("never_seen", provision=False) is None
        assert superset.dashboards_created == 0


class TestDegradeAndRepair:
    def test_unreachable_superset_still_persists_a_binding(self, superset):
        """The form has to render. A partial binding keeps the name
        stable so `repair` can finish the job later."""
        superset.reachable = False
        binding = pv.resolve_dashboard("offline_dash")

        assert binding is not None
        assert binding["embed_uuid"] is None
        assert binding["superset_dashboard_id"] is None

    def test_repair_completes_a_partial_binding(self, superset):
        """Recovery path once Superset comes back."""
        superset.reachable = False
        pv.resolve_dashboard("offline_dash")

        superset.reachable = True
        SupersetClient._token_cache.clear()
        repaired = pv.repair_dashboard("offline_dash")

        assert repaired["superset_dashboard_id"] is not None
        assert repaired["embed_uuid"] is not None
        assert repaired["filter_id"] is not None

    def test_repair_of_an_unknown_name_is_an_error(self, superset):
        """Repair is an explicit user action; silence would be unhelpful."""
        with pytest.raises(Exception) as excinfo:
            pv.repair_dashboard("no_such_binding")
        assert "No dashboard binding" in str(excinfo.value)


class TestAllowedDomains:
    def test_default_is_unrestricted_and_documented(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Superset treats an empty allowed_domains as "any domain may
        embed" (`is_referrer_allowed = not embedded.allowed_domains`).
        That is a real exposure, so the emptiness must be deliberate and
        visible rather than an accident of configuration."""
        monkeypatch.delenv("FRONTFLOW_PUBLIC_ORIGIN", raising=False)
        assert pv.allowed_domains() == []

    def test_public_origin_restricts_embedding(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv(
            "FRONTFLOW_PUBLIC_ORIGIN", "https://forms.example.com, https://b.test"
        )
        assert pv.allowed_domains() == [
            "https://forms.example.com",
            "https://b.test",
        ]


class TestBindingStorage:
    def test_partial_update_preserves_earlier_fields(self, app):
        """Provisioning fills these in over several calls, and a later
        pass may only manage some of them. A partial update that cleared
        the rest would undo the work of an earlier pass."""
        store.upsert_dashboard_binding(
            name="d",
            connection_name="superset_default",
            superset_dashboard_id="7",
            auto_created=True,
        )
        updated = store.upsert_dashboard_binding(
            name="d", connection_name="superset_default", embed_uuid="U"
        )

        assert updated["superset_dashboard_id"] == "7"
        assert updated["auto_created"] is True
        assert updated["embed_uuid"] == "U"

    def test_delete_only_forgets_locally(self, app):
        """Removing a binding must not imply deleting the dashboard in
        Superset — that is a separate, explicit decision."""
        store.upsert_dashboard_binding(
            name="d", connection_name="superset_default"
        )
        assert store.delete_dashboard_binding("d") is True
        assert store.delete_dashboard_binding("d") is False
        assert store.get_dashboard_binding("d") is None


class TestOptionalExtra:
    """Superset support is `pip install frontflow[superset]`.

    An install that never touches Superset must be completely
    unaffected, which means nothing here may be imported at package
    load. These run in a subprocess so a module another test already
    imported cannot mask the result.
    """

    def test_package_import_does_not_pull_in_superset(self):
        import subprocess
        import sys

        code = (
            "import sys, frontflow; "
            "leaked = [m for m in sys.modules if m.startswith('frontflow.superset')]; "
            "assert not leaked, f'superset modules imported at package load: {leaked}'"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    def test_package_import_does_not_pull_in_httpx(self):
        """httpx is a core dependency today, but the Superset module must
        not be what drags it in — that coupling is what would make the
        extra meaningless."""
        import subprocess
        import sys

        code = (
            "import sys, frontflow; "
            "import frontflow.dsl.store; "
            "assert 'frontflow.superset.client' not in sys.modules"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
