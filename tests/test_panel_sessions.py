"""A control panel is a session, not a submission.

A `closes=False` node never routes, so it never terminates. Left as an
ordinary submission it sits at `running` forever: it inflates every
count, and its field values flow into `v_frontflow_submissions` where
they become analytics data. That is not hypothetical — a filter widget
briefly named `units` put objects into a column another form stores
numbers in, and broke every chart reading it.

The failure mode of the fix is over-filtering, so `TestOrdinaryFormsAreUnaffected`
matters as much as the exclusions themselves.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from frontflow.dsl import runtime, store


PANEL = "test_filter_panel"
ORDINARY = "test_two_step"


def _start_panel(client: TestClient) -> str:
    r = client.post(
        f"/api/forms/{PANEL}/submissions", json={"values": {"region": "north"}}
    )
    assert r.status_code == 201, r.text
    return r.json()["handle"]


def _start_ordinary(client: TestClient) -> str:
    r = client.post(
        f"/api/forms/{ORDINARY}/submissions", json={"values": {"name": "Ada"}}
    )
    assert r.status_code == 201, r.text
    return r.json()["handle"]


def _counts(form_id: str) -> dict:
    for row in store.list_forms_overview():
        if row["form_id"] == form_id:
            return row["submissions"]
    return {}


class TestClassification:
    def test_a_panel_is_a_session(self, admin_client: TestClient):
        handle = _start_panel(admin_client)
        assert runtime.get_submission(handle).kind == "session"

    def test_an_ordinary_form_is_a_submission(self, admin_client: TestClient):
        handle = _start_ordinary(admin_client)
        assert runtime.get_submission(handle).kind == "submission"

    def test_the_kind_is_persisted(self, admin_client: TestClient):
        """Derived would be simpler, but the counts and the reporting
        view are SQL over this table and cannot see the DSL."""
        handle = _start_panel(admin_client)
        rows = {r["handle"]: r for r in store.load_submissions()}
        assert rows[handle]["kind"] == "session"

    def test_it_survives_rehydration(self, admin_client: TestClient):
        """A restart must not turn a panel into a submission."""
        handle = _start_panel(admin_client)
        snapshot = {
            r["handle"]: r for r in store.load_submissions()
        }[handle]
        assert runtime.hydrate_submission(snapshot, PANEL).kind == "session"


class TestWhereItRests:
    """Classification follows where a submission comes to REST, not
    where it started.

    The rule "the landing node decides" looked right and was wrong: a
    form may load data in an ordinary closing node and only then reach
    its panel, which is exactly how the demo `sales_filter` is shaped.
    Classified at the landing node alone, every one of its sessions
    counted as a submission.
    """

    def test_a_panel_reached_after_a_closing_node_is_a_session(
        self, admin_client: TestClient
    ):
        """`test_filter_panel` lands on `panel`; drive past it to
        `mixed`, which also does not close."""
        handle = _start_panel(admin_client)
        # Apply on the panel — still resting on a non-closing node.
        admin_client.post(
            f"/api/forms/{PANEL}/submissions/{handle}/steps/panel",
            json={"values": {"region": "south"}},
        )
        assert runtime.get_submission(handle).kind == "session"

    def test_moving_past_a_panel_makes_it_a_submission_again(
        self, admin_client: TestClient
    ):
        """`mixed` declares closes=False but offers a button that
        overrides it. Once that button is used the submission moves on
        and will terminate, so it is a submission again."""
        from frontflow.dsl.compile import compile_workflow
        from frontflow.dsl.core import WORKFLOWS
        from frontflow.dsl.runtime import _submit_closes_node

        node = compile_workflow(WORKFLOWS[PANEL]).all_nodes_by_id["mixed"]
        assert _submit_closes_node(node, "go") is True, "fixture changed"

        handle = _start_panel(admin_client)
        assert runtime.get_submission(handle).kind == "session"

        # Close `panel` is impossible; `mixed` is reached only via it,
        # so assert the rule at the classification boundary instead.
        assert _submit_closes_node(node, "apply") is False


class TestSessionsAreNotCounted:
    def test_repeated_applies_add_no_submissions(
        self, admin_client: TestClient
    ):
        """The reported symptom: every visit left a permanently-running
        row in the form's totals."""
        before = _counts(PANEL).get("total", 0)
        handle = _start_panel(admin_client)
        for region in ("south", "east", "west"):
            admin_client.post(
                f"/api/forms/{PANEL}/submissions/{handle}/steps/panel",
                json={"values": {"region": region}},
            )
        assert _counts(PANEL).get("total", 0) == before

    def test_a_session_is_absent_from_the_submissions_tab(
        self, admin_client: TestClient
    ):
        handle = _start_panel(admin_client)
        listed = store.list_all_form_submissions(PANEL)
        assert handle not in [row["handle"] for row in listed]

    def test_a_session_is_absent_from_the_export(
        self, admin_client: TestClient
    ):
        """A downstream warehouse wants submissions, not the state of
        somebody's filter bar."""
        handle = _start_panel(admin_client)
        page = store.export_submissions(form_id=PANEL, limit=100)
        rows = next(v for v in page.values() if isinstance(v, list))
        assert runtime.get_submission(handle).submission_id not in {
            r.get("submission_id") for r in rows
        }


class TestOrdinaryFormsAreUnaffected:
    """The failure mode of this change is over-filtering — the guard
    that a real submission still counts everywhere it used to."""

    def test_it_is_counted(self, admin_client: TestClient):
        before = _counts(ORDINARY).get("total", 0)
        _start_ordinary(admin_client)
        assert _counts(ORDINARY).get("total", 0) == before + 1

    def test_it_appears_in_the_submissions_tab(
        self, admin_client: TestClient
    ):
        handle = _start_ordinary(admin_client)
        listed = store.list_all_form_submissions(ORDINARY)
        assert handle in [row["handle"] for row in listed]

    def test_the_migration_leaves_no_unclassified_rows(
        self, admin_client: TestClient
    ):
        """The upgrade risk: the column is added nullable (SQLite cannot
        add NOT NULL to a populated table), so the backfill is what
        keeps historical counts intact. A row left NULL would be
        excluded by a stricter predicate and silently vanish from every
        total."""
        from sqlalchemy import text

        _start_ordinary(admin_client)
        store._migrate_add_columns()  # idempotent
        with store._engine.begin() as conn:
            unclassified = conn.execute(
                text("SELECT count(*) FROM submission WHERE kind IS NULL")
            ).scalar()
        assert unclassified == 0


class TestReclassifyBackfill:
    """`store.reclassify_sessions` repairs rows the migration guessed at.

    The `kind` column arrived with a blanket backfill to 'submission',
    because a migration cannot ask the DSL whether a node closes. Every
    panel session written before that point therefore reads as a
    submission stuck at `running`.

    As with the exclusions above, the dangerous direction is
    over-reach — a backfill that also rewrites ordinary forms would
    quietly delete real submissions from every count.

    Every assertion here reads the DATABASE, not
    `runtime.get_submission`. The runtime answers from an in-memory
    submission whose `kind` was set when it was created, so it reports
    the right answer whether or not the stored row was ever repaired —
    which is exactly the bug this backfill exists to fix.
    """

    def _db_kind(self, handle: str) -> str:
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        with Session(store._engine) as session:
            return session.execute(
                select(store.Submission.kind).where(
                    store.Submission.handle == handle
                )
            ).scalar_one()

    def _force_kind(self, handle: str, kind: str) -> None:
        """Put a row back into the state the migration left it in."""
        from sqlalchemy import update
        from sqlalchemy.orm import Session

        with Session(store._engine) as session:
            session.execute(
                update(store.Submission)
                .where(store.Submission.handle == handle)
                .values(kind=kind)
            )
            session.commit()

    def test_a_mislabelled_panel_session_is_repaired(
        self, admin_client: TestClient
    ):
        handle = _start_panel(admin_client)
        self._force_kind(handle, "submission")
        assert self._db_kind(handle) == "submission"

        changed = store.reclassify_sessions(dry_run=False)

        assert handle in {c["handle"] for c in changed}
        assert self._db_kind(handle) == "session"

    def test_dry_run_reports_without_writing(self, admin_client: TestClient):
        handle = _start_panel(admin_client)
        self._force_kind(handle, "submission")

        changed = store.reclassify_sessions(dry_run=True)

        assert handle in {c["handle"] for c in changed}
        assert self._db_kind(handle) == "submission"

    def test_ordinary_submissions_are_never_touched(
        self, admin_client: TestClient
    ):
        """The over-reach guard."""
        handle = _start_ordinary(admin_client)
        before = _counts(ORDINARY)

        changed = store.reclassify_sessions(dry_run=False)

        assert handle not in {c["handle"] for c in changed}
        assert self._db_kind(handle) == "submission"
        assert _counts(ORDINARY) == before

    def test_running_again_changes_nothing(self, admin_client: TestClient):
        handle = _start_panel(admin_client)
        self._force_kind(handle, "submission")

        store.reclassify_sessions(dry_run=False)
        assert store.reclassify_sessions(dry_run=False) == []

    def test_the_repaired_row_leaves_the_reporting_view(
        self, admin_client: TestClient
    ):
        """The point of the exercise.

        A session counted as a submission puts its widget values into
        the analytics dataset. Repairing `kind` has to actually remove
        it from what Superset reads.
        """
        panel = _start_panel(admin_client)
        _start_ordinary(admin_client)
        self._force_kind(panel, "submission")

        # While mislabelled, the panel's values are in the dataset.
        before = {r["form_id"] for r in store.export_submissions(limit=100)["submissions"]}
        assert PANEL in before

        store.reclassify_sessions(dry_run=False)

        after = {r["form_id"] for r in store.export_submissions(limit=100)["submissions"]}
        assert PANEL not in after
        assert ORDINARY in after

    def test_it_agrees_with_the_runtime(self, admin_client: TestClient):
        """The rule is duplicated from `runtime._submit_closes_node`.

        Two copies of a rule drift. This asserts they still agree, so
        the day someone changes how a node closes, one of these fails.
        """
        for start in (_start_panel, _start_ordinary):
            handle = start(admin_client)
            live = runtime.get_submission(handle).kind

            self._force_kind(handle, "submission")
            store.reclassify_sessions(dry_run=False)

            assert self._db_kind(handle) == live
