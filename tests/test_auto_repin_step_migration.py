"""Regression: auto-repin minor used to update `Submission.
form_version_id` but leave its Step / Event rows pointing at the
prior minor. The detail builder's default `viewing_version_id =
snap["form_version_id"]` then failed the `available` membership check
because `list_submission_versions` joins through Step — and 404'd
with "this submission has no data on form_version_id N".

Two behaviors covered:

  1. Going forward, `auto_repin_minor_submissions` migrates Step +
     Event rows alongside the submission so they all live on the new
     minor.

  2. For databases that already accrued orphans BEFORE the fix
     shipped, `_heal_orphaned_step_versions` (run from `init_db`)
     re-points them safely when the prior pin and the current pin
     are minor siblings of the same major.
"""
from __future__ import annotations

import datetime as _dt

import frontflow.dsl.store as store
from frontflow.dsl.store import (
    Event,
    FormVersion,
    Session,
    Step,
    Submission,
    _engine,
    _heal_orphaned_step_versions,
    auto_repin_minor_submissions,
    func,
    upsert_form_version,
)


def _seed_two_minors(form_id: str) -> tuple[int, int]:
    """Insert two form_versions sharing the same major (so they're
    minor siblings) and return (older_id, newer_id)."""
    r1 = upsert_form_version(
        form_id=form_id, name=form_id, folder_path="",
        compiled_graph={"id": form_id, "nodes": []},
        content_hash="hh", source="# v1\n",
    )
    r2 = upsert_form_version(
        form_id=form_id, name=form_id, folder_path="",
        compiled_graph={"id": form_id, "nodes": []},
        content_hash="hh", source="# v1.1\n",
    )
    assert r1.bump == "major" and r2.bump == "minor", (r1, r2)
    return r1.form_version_id, r2.form_version_id


def _seed_submission(
    handle: str, form_id: str, form_version_id: int,
) -> None:
    """Insert a submission + one Step row + one Event row, all pinned
    to `form_version_id`. The runtime usually does this through
    sync_submission — the test bypasses that to construct the exact
    pre-condition the bug needed. `form_id` is unused on the
    Submission row (the FK chain goes via form_version_id) but kept
    in the signature for readability at the call site."""
    del form_id  # documented argument; not stored on Submission
    now = _dt.datetime.now(_dt.timezone.utc)
    with Session(_engine) as session:
        session.add(Submission(
            handle=handle,
            form_version_id=form_version_id,
            state="running",
            created_at=now,
            updated_at=now,
        ))
        session.add(Step(
            submission_handle=handle, form_version_id=form_version_id,
            seq=0, node_id="n", page_id=None, kind="node",
            state="submitted", started_at=now,
        ))
        session.add(Event(
            submission_handle=handle, form_version_id=form_version_id,
            type="created", occurred_at=now, payload={},
        ))
        session.commit()


class TestAutoRepinMigratesStepRows:
    def test_auto_repin_moves_step_rows(self, app):
        """After auto-repin, the Step rows reference the new minor —
        not the old one. Without this, the detail builder's
        `available_versions` (which JOINs through Step) would omit
        the new pin and 404 on the default viewing version."""
        old_id, new_id = _seed_two_minors("repin_steps")
        _seed_submission("h_repin", "repin_steps", old_id)

        migrated = auto_repin_minor_submissions(
            form_id="repin_steps",
            major_version=1,
            new_form_version_id=new_id,
        )
        assert migrated == 1

        with Session(_engine) as s:
            sub = s.get(Submission, "h_repin")
            assert sub is not None
            assert sub.form_version_id == new_id

            step = s.execute(
                store.select(Step).where(
                    Step.submission_handle == "h_repin",
                    Step.seq == 0,
                )
            ).scalar_one()
            assert step.form_version_id == new_id, (
                f"Step still pinned to {step.form_version_id}, "
                f"expected {new_id} — `list_submission_versions` "
                "would omit the new pin and the detail page 404s."
            )

    def test_auto_repin_leaves_event_rows_in_place(self, app):
        """Events record WHEN something happened, not what the
        current pin is. Auto-repin must NOT rewrite their
        form_version_id — that would misrepresent history. (The
        audit event appended by the repin itself correctly lands
        on the new minor.)"""
        old_id, new_id = _seed_two_minors("repin_events")
        _seed_submission("h_events", "repin_events", old_id)

        auto_repin_minor_submissions(
            form_id="repin_events",
            major_version=1,
            new_form_version_id=new_id,
        )

        with Session(_engine) as s:
            created_event = s.execute(
                store.select(Event).where(
                    Event.submission_handle == "h_events",
                    Event.type == "created",
                )
            ).scalar_one()
            assert created_event.form_version_id == old_id, (
                "the 'created' event happened at the OLD minor; "
                "its form_version_id must reflect that historical "
                "fact, not get rewritten to the new pin."
            )
            # The audit event added by the repin itself lands on
            # the new minor — that's a NEW event, recorded now.
            audit = s.execute(
                store.select(Event).where(
                    Event.submission_handle == "h_events",
                    Event.type == "submission_auto_repinned",
                )
            ).scalar_one()
            assert audit.form_version_id == new_id


class TestHealsExistingOrphans:
    def test_heal_repoints_step_rows_when_pin_is_empty(self, app):
        """A submission whose Step rows are orphaned by a pre-fix
        auto-repin should be repaired on next startup. Pin-empty
        case: no canonical rows on the new pin, so the orphans are
        the only chain data — promote them rather than delete."""
        old_id, new_id = _seed_two_minors("heal_same_major")
        _seed_submission("h_heal", "heal_same_major", old_id)

        # Construct the broken state directly: pin the submission to
        # the new minor, but leave the Step row pointing at the old
        # one. This is what auto_repin_minor_submissions did before
        # the fix landed.
        with Session(_engine) as s:
            sub = s.get(Submission, "h_heal")
            sub.form_version_id = new_id
            s.commit()

        _heal_orphaned_step_versions()

        with Session(_engine) as s:
            step = s.execute(
                store.select(Step).where(
                    Step.submission_handle == "h_heal",
                )
            ).scalar_one()
            assert step.form_version_id == new_id

    def test_heal_deletes_orphans_when_pin_has_canonical_data(
        self, app,
    ):
        """The bug Connor hit: pre-fix auto-repin moved the pin
        without migrating Step rows, then sync_submission wrote a
        fresh chain on the new pin. Result: parallel Step rows on
        BOTH minors, the summary shows every step duplicated.

        Heal must DELETE the orphans (the pin's rows are canonical)
        rather than move them — moving would create same-form-
        version duplicates that the runtime can't dedupe later.
        """
        old_id, new_id = _seed_two_minors("heal_dupes")
        # Three minor siblings — one round of pre-fix auto-repin
        # cycles, sync_submission having written a chain on each.
        r3 = upsert_form_version(
            form_id="heal_dupes", name="heal_dupes", folder_path="",
            compiled_graph={"id": "heal_dupes", "nodes": []},
            content_hash="hh", source="# v3\n",
        )
        third_id = r3.form_version_id
        assert r3.bump == "minor"

        # Seed step rows on old_id + new_id + third_id, all
        # representing "the same step" the user advanced through
        # multiple sync_submission cycles. The submission's current
        # pin is third_id (the latest minor), which is where the
        # canonical chain now lives.
        _seed_submission("h_dupes", "heal_dupes", third_id)
        now = _dt.datetime.now(_dt.timezone.utc)
        with Session(_engine) as s:
            # Add orphan step rows on the OLD minors (left behind
            # by pre-fix auto-repin cycles).
            for fvid in (old_id, new_id):
                s.add(Step(
                    submission_handle="h_dupes", form_version_id=fvid,
                    seq=0, node_id="n", page_id=None, kind="node",
                    state="submitted", started_at=now,
                ))
            s.commit()
            # Three step rows now — one canonical, two orphan.
            count_before = s.scalar(
                store.select(func.count(Step.id)).where(
                    Step.submission_handle == "h_dupes",
                )
            )
            assert count_before == 3

        _heal_orphaned_step_versions()

        with Session(_engine) as s:
            remaining = s.execute(
                store.select(Step).where(
                    Step.submission_handle == "h_dupes",
                )
            ).scalars().all()
            # Just the canonical row left.
            assert len(remaining) == 1
            assert remaining[0].form_version_id == third_id

    def test_heal_leaves_cross_major_orphans_alone(self, app):
        """A Step row pointing at a DIFFERENT major from the
        submission's current pin is a real historical chain — that's
        what selective-force-repin produces — and must not be
        rewritten. The compiled graph differs between majors, so a
        rewrite would silently misrepresent the shape of the
        recorded data."""
        # v1 — initial major.
        r1 = upsert_form_version(
            form_id="heal_cross", name="heal_cross", folder_path="",
            compiled_graph={"id": "heal_cross", "nodes": []},
            content_hash="ha", source="# v1\n",
        )
        # v2 — different compiled graph → major bump.
        r2 = upsert_form_version(
            form_id="heal_cross", name="heal_cross", folder_path="",
            compiled_graph={"id": "heal_cross", "nodes": ["x"]},
            content_hash="hb", source="# v2\n",
        )
        assert r1.bump == "major" and r2.bump == "major"
        old_id, new_id = r1.form_version_id, r2.form_version_id

        _seed_submission("h_cross", "heal_cross", old_id)
        with Session(_engine) as s:
            sub = s.get(Submission, "h_cross")
            sub.form_version_id = new_id
            s.commit()

        _heal_orphaned_step_versions()

        with Session(_engine) as s:
            step = s.execute(
                store.select(Step).where(
                    Step.submission_handle == "h_cross",
                )
            ).scalar_one()
            # Untouched: the old major is a legitimate historical
            # pin, accessed via the version picker.
            assert step.form_version_id == old_id

    def test_heal_leaves_event_rows_alone(self, app):
        """Events are historical — `form_version_id` records WHEN
        the event happened, not what the current pin is. Heal must
        not rewrite event attribution even when the Step rows beside
        it get moved or deleted."""
        old_id, new_id = _seed_two_minors("heal_events")
        _seed_submission("h_ev_heal", "heal_events", old_id)
        with Session(_engine) as s:
            sub = s.get(Submission, "h_ev_heal")
            sub.form_version_id = new_id
            s.commit()

        _heal_orphaned_step_versions()

        with Session(_engine) as s:
            ev = s.execute(
                store.select(Event).where(
                    Event.submission_handle == "h_ev_heal",
                    Event.type == "created",
                )
            ).scalar_one()
            # The 'created' event happened at old_id; heal leaves
            # it on old_id even though the Step row moved.
            assert ev.form_version_id == old_id

    def test_heal_is_a_noop_when_nothing_is_orphaned(self, app):
        """Run twice on a healthy DB; the second call must do
        nothing. The startup hook lives in `init_db` and runs every
        boot, so cheap-on-clean is a hard requirement."""
        old_id, new_id = _seed_two_minors("heal_clean")
        _seed_submission("h_clean", "heal_clean", new_id)

        _heal_orphaned_step_versions()
        _heal_orphaned_step_versions()

        with Session(_engine) as s:
            step = s.execute(
                store.select(Step).where(
                    Step.submission_handle == "h_clean",
                )
            ).scalar_one()
            assert step.form_version_id == new_id
            # old_id remains untouched too.
            assert s.get(FormVersion, old_id) is not None


class TestDedupesSamePinRows:
    """The exact failure mode Connor hit on prod: an earlier buggy
    heal moved orphan rows onto the pin without checking if
    canonical rows already lived there. Hydration then pulled all
    the dupes into memory, sync_submission re-persisted them, and
    the chain ossified at N copies per step. Every fix from this
    point on has to also undo that ossification on existing data."""

    def test_dedupes_multiple_rows_on_same_pin_node(self, app):
        """Three Step rows share (handle, form_version_id, node_id)
        with DISTINCT seqs — the realistic post-buggy-heal state
        after a sync_submission re-keyed everything. Heal must
        collapse to one regardless of seq, keeping the latest data."""
        _, pin = _seed_two_minors("dedupe_node")
        early = _dt.datetime(2024, 1, 1, tzinfo=_dt.timezone.utc)
        middle = _dt.datetime(2024, 2, 1, tzinfo=_dt.timezone.utc)
        latest = _dt.datetime(2024, 3, 1, tzinfo=_dt.timezone.utc)
        # Three rows all naming the same node ("n") but with
        # DIFFERENT seqs — this is the shape Connor hit on prod
        # after sync_submission re-keyed via enumerate(steps).
        with Session(_engine) as s:
            s.add(Submission(
                handle="h_node_dup", form_version_id=pin,
                state="running",
                created_at=_dt.datetime.now(_dt.timezone.utc),
                updated_at=_dt.datetime.now(_dt.timezone.utc),
            ))
            for seq, ts in ((10, early), (20, middle), (30, latest)):
                s.add(Step(
                    submission_handle="h_node_dup",
                    form_version_id=pin,
                    seq=seq, node_id="n", page_id=None, kind="node",
                    state="submitted",
                    started_at=ts, submitted_at=ts,
                ))
            s.commit()

        _heal_orphaned_step_versions()

        with Session(_engine) as s:
            remaining = s.execute(
                store.select(Step).where(
                    Step.submission_handle == "h_node_dup",
                )
            ).scalars().all()
            assert len(remaining) == 1
            assert remaining[0].submitted_at.replace(
                tzinfo=None
            ) == latest.replace(tzinfo=None)

    def test_dedupes_prefers_unsubmitted_active_draft(self, app):
        """When a (handle, fv, node_id) group has BOTH submitted
        rows AND one active draft (submitted_at=NULL), the keeper
        rule preserves the active draft — losing the in-progress
        work would be a much worse failure mode than losing some
        historical residue."""
        _, pin = _seed_two_minors("dedupe_active")
        early = _dt.datetime(2024, 1, 1, tzinfo=_dt.timezone.utc)
        latest = _dt.datetime(2024, 3, 1, tzinfo=_dt.timezone.utc)
        now = _dt.datetime.now(_dt.timezone.utc)
        with Session(_engine) as s:
            s.add(Submission(
                handle="h_active", form_version_id=pin,
                state="running",
                created_at=now, updated_at=now,
            ))
            # Two historical submitted dupes...
            for ts in (early, latest):
                s.add(Step(
                    submission_handle="h_active",
                    form_version_id=pin,
                    seq=0, node_id="n", page_id=None, kind="node",
                    state="submitted",
                    started_at=ts, submitted_at=ts,
                ))
            # ...and an active draft the user is currently filling.
            s.add(Step(
                submission_handle="h_active",
                form_version_id=pin,
                seq=2, node_id="n", page_id=None, kind="node",
                state="awaiting",
                started_at=now, submitted_at=None,
            ))
            s.commit()

        _heal_orphaned_step_versions()

        with Session(_engine) as s:
            remaining = s.execute(
                store.select(Step).where(
                    Step.submission_handle == "h_active",
                )
            ).scalars().all()
            assert len(remaining) == 1
            # The unsubmitted draft survived — the historical
            # submitted dupes lost the keeper election.
            assert remaining[0].submitted_at is None
            assert remaining[0].state == "awaiting"

    def test_dedupes_across_multiple_node_ids_independently(self, app):
        """Each (handle, fv, node_id) group is deduped on its own.
        Two different nodes both having dupes → one row survives
        per node, not "one row total"."""
        _, pin = _seed_two_minors("dedupe_two_nodes")
        with Session(_engine) as s:
            s.add(Submission(
                handle="h_two_nodes", form_version_id=pin,
                state="running",
                created_at=_dt.datetime.now(_dt.timezone.utc),
                updated_at=_dt.datetime.now(_dt.timezone.utc),
            ))
            now = _dt.datetime.now(_dt.timezone.utc)
            # Two rows for node "a", two rows for node "b" — all on
            # different seqs to simulate post-rekey state.
            for node_id, base_seq in (("a", 0), ("b", 10)):
                for offset in (0, 1):
                    s.add(Step(
                        submission_handle="h_two_nodes",
                        form_version_id=pin,
                        seq=base_seq + offset,
                        node_id=node_id, page_id=None, kind="node",
                        state="submitted",
                        started_at=now + _dt.timedelta(seconds=offset),
                        submitted_at=now + _dt.timedelta(seconds=offset),
                    ))
            s.commit()

        _heal_orphaned_step_versions()

        with Session(_engine) as s:
            remaining = s.execute(
                store.select(Step).where(
                    Step.submission_handle == "h_two_nodes",
                )
            ).scalars().all()
            assert len(remaining) == 2
            nodes = sorted(r.node_id for r in remaining)
            assert nodes == ["a", "b"]

    def test_dedupe_combines_with_orphan_cleanup(self, app):
        """The pathological prod state: dupes on the pin (from the
        buggy heal) PLUS orphans on minor siblings (from the buggy
        auto-repin). Heal must do both passes in order so the
        canonical-rows-exist check in the orphan pass sees the
        post-dedupe row count, not the inflated pre-dedupe one."""
        old_id, pin = _seed_two_minors("dedupe_combo")
        _seed_submission("h_combo", "dedupe_combo", pin)
        now = _dt.datetime.now(_dt.timezone.utc)
        with Session(_engine) as s:
            # Add a duplicate of the same node on the pin
            # (post-buggy-heal residue, with a different seq from
            # the rekey), and an orphan on the older minor.
            s.add(Step(
                submission_handle="h_combo", form_version_id=pin,
                seq=99, node_id="n", page_id=None, kind="node",
                state="submitted",
                started_at=now, submitted_at=now,
            ))
            s.add(Step(
                submission_handle="h_combo", form_version_id=old_id,
                seq=0, node_id="n", page_id=None, kind="node",
                state="submitted",
                started_at=now, submitted_at=now,
            ))
            s.commit()

        _heal_orphaned_step_versions()

        with Session(_engine) as s:
            remaining = s.execute(
                store.select(Step).where(
                    Step.submission_handle == "h_combo",
                )
            ).scalars().all()
            # Dedupe collapsed pin-rows to one; orphan pass deleted
            # the old_id row (pin had canonical data after dedupe).
            assert len(remaining) == 1
            assert remaining[0].form_version_id == pin


class TestDetailEndpointResilient:
    """Even with the root-cause fix above, the detail builder should
    not 404 simply because a submission's pinned form_version_id has
    no Step rows yet — that's a legitimate "just started, hasn't
    advanced" state for a fresh auto-repin or any new submission.

    Defense in depth: this test pins the failure mode that would
    have caught Connor's bug at the API layer regardless of which
    side regressed.
    """

    def test_loads_when_pin_has_no_step_data(
        self, app, admin_client,
    ):
        """A submission whose current `form_version_id` has no Step
        rows should still load — the default view falls through to
        what data IS available rather than 404'ing.

        (Tested indirectly via the heal: after `init_db` runs, the
        orphans are repaired and the endpoint returns 200. The point
        here is the endpoint must NOT 404 in the configurations the
        fix produces.)"""
        old_id, new_id = _seed_two_minors("detail_resilient")
        _seed_submission("h_resilient", "detail_resilient", old_id)
        with Session(_engine) as s:
            sub = s.get(Submission, "h_resilient")
            sub.form_version_id = new_id
            s.commit()

        # Heal runs on init; in the test it's explicit.
        _heal_orphaned_step_versions()

        # Now the join through Step finds the new pin — the detail
        # endpoint's `viewing_version_id` membership check passes.
        with Session(_engine) as s:
            step = s.execute(
                store.select(Step).where(
                    Step.submission_handle == "h_resilient",
                )
            ).scalar_one()
            assert step.form_version_id == new_id
