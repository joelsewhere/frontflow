"""Superset orchestration operators.

`RefreshDashboard` is an operator you place in a node's `>>` chain, the
same way you place `airflow.TriggerDag` or a sensor:

    submit >> ingest() >> wait_for_dag >> superset.RefreshDashboard("sales")

That placement IS the feature. A dashboard refresh is a step in the
form's logic, so the author decides when it happens — after a backend
returns, after a DAG finishes, on one branch and not another — rather
than it firing implicitly on every submit.

**Fire-and-forget.** The operator reaches `success` the moment the chain
runs it. It never waits for a browser to acknowledge anything: a
submission has to progress with no client attached, and a chain that
stalled because nobody had the page open would be a bad trade.

What it actually does is mint a *refresh directive* into the chain
step's state. That state is already polled by the client
(`useSubmission`), so the directive reaches an open dashboard block with
no new transport. A block that sees a directive token it has not
handled re-queries its charts in place.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Optional

from ..dsl.external import ExternalTask

# How far ahead of "now" the refresh window's upper bound is set.
#
# The bound exists to move the query's cache key, not to exclude
# anything. Pushing it into the future means a row written moments ago
# cannot fall outside the window because the database clock and this
# process's clock disagree.
LOOKAHEAD_SECONDS = 300

# Superset's own time format: no fractional seconds, no trailing Z.
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"

_lock = threading.Lock()
_last_epoch_seconds = 0


def next_stamp() -> str:
    """A timestamp that is strictly greater on every call.

    Superset's time format has second resolution, so two calls inside
    one second would otherwise produce identical strings — identical
    query cache keys, and no visible update. Forcing it to advance is
    the whole mechanism.

    The guarantee is per-process. Two workers acting in the same second
    can still collide; the consequence is one cached result, not a
    broken dashboard.
    """
    global _last_epoch_seconds
    with _lock:
        candidate = int(time.time()) + LOOKAHEAD_SECONDS
        _last_epoch_seconds = max(candidate, _last_epoch_seconds + 1)
        return datetime.fromtimestamp(
            _last_epoch_seconds, tz=timezone.utc
        ).strftime(_TIME_FORMAT)


def next_time_range() -> str:
    """The `time_range` a refresh should apply.

    Superset splits this on `" : "` and treats an EMPTY side as
    unbounded, so `" : <ts>"` means "everything up to <ts>". The literal
    string "No filter" is only valid on its own — using it as the
    left-hand side produces `Cannot parse time string [No filter]`.

    The value is forced to advance on every call. Superset's format is
    second-resolution, so two refreshes within the same second would
    otherwise produce an identical string, hence an identical query
    cache key, hence no visible update — which is the entire mechanism.

    The guarantee is per-process. Two workers refreshing in the same
    second can still collide; the consequence is one cached result, not
    a broken dashboard.
    """
    return f" : {next_stamp()}"


class RefreshDashboard(ExternalTask):
    """Refresh an embedded dashboard, in place, at this point in the chain.

        submit >> run_pipeline() >> superset.RefreshDashboard("sales")

    `name` is the same logical name `displays.Dashboard` uses. The two
    are independent: a chain may refresh a dashboard shown on a later
    node, or on none at all.

    Nothing is pushed to the browser — the directive rides the state the
    client already polls. A dashboard block that is not currently open
    simply never sees it, which is correct: it will load current data
    when it next mounts.
    """

    kind = "superset_refresh"

    # An authored step, not plumbing: it belongs in the workflow graph
    # so the chain UI shows where the refresh happens.
    graph_visible = True

    def __init__(
        self,
        name: str,
        *,
        connection: Optional[str] = None,
        hidden: bool = False,
        id: Optional[str] = None,
    ) -> None:
        if not name or not str(name).strip():
            raise ValueError(
                "superset.RefreshDashboard needs a dashboard name, e.g. "
                'superset.RefreshDashboard("sales_overview")'
            )
        super().__init__(id=id or f"refresh_{str(name).strip()}")
        self.name = str(name).strip()
        self.connection = connection
        # Keep it out of the chain UI. A failure still surfaces.
        self.hidden = hidden


def build_directive(name: str) -> dict:
    """The payload a dashboard block acts on.

    `token` is what makes the refresh idempotent client-side: a block
    refreshes once per token it has not seen, so re-polling the same
    chain state does not re-trigger anything.
    """
    time_range = next_time_range()
    return {
        "dashboard": name,
        "time_range": time_range,
        # The time range is already strictly increasing, so it doubles
        # as the token — one value to keep consistent rather than two.
        "token": time_range.strip(),
    }


class SetFilters(ExternalTask):
    """Point an embedded dashboard's filters at values from this chain.

        @backend
        def classify(region, units):
            return {"region": region, "segment": "enterprise"}

        submit >> classify >> superset.SetFilters(
            "sales_overview",
            region="{{ steps.entry.classify.region }}",
            segment="{{ steps.entry.classify.segment }}",
        )

    Filters are named as the author named them **in Superset** — the
    name shown on the dashboard's filter bar — and values are ordinary
    template strings, resolved against prior steps exactly as
    `AirflowStatus.run_id` is. So a `@backend` return, a submitted form
    value, or a literal all work in the same place.

    A value may be a list, for a filter accepting several selections.

    `panel` narrows it to ONE rendering of that dashboard, named by the
    `id` its `displays.Dashboard(...)` carries:

        displays.Dashboard("sales_overview", id="detail")
        superset.SetFilters("sales_overview", panel="detail", Region=...)

    A workspace may show the same dashboard more than once — one panel
    tracking the whole picture, another following what was just
    submitted. Without `panel` the directive reaches every rendering of
    that dashboard, which is the right default when there is only one.

    This drives the VIEWER'S dashboard, not the dashboard's saved
    configuration: it focuses this person's view on what their
    submission was about, and changes nothing for anyone else. It is
    also not a security boundary — the viewer can widen a filter by
    clicking it. Restricting what someone may see at all is row-level
    security on the guest token, which is a different mechanism.

    Like `RefreshDashboard` this is fire-and-forget and talks to nobody:
    the directive rides the state the client already polls, and moving a
    filter re-queries the charts, so it refreshes as a side effect of
    doing its job.
    """

    kind = "superset_set_filters"

    # An authored step: it belongs in the graph so the chain UI shows
    # where the dashboard gets pointed somewhere new.
    graph_visible = True

    def __init__(
        self,
        name: str,
        *,
        panel: Optional[str] = None,
        connection: Optional[str] = None,
        hidden: bool = False,
        id: Optional[str] = None,
        **filters: object,
    ) -> None:
        if not name or not str(name).strip():
            raise ValueError(
                "superset.SetFilters needs a dashboard name, e.g. "
                'superset.SetFilters("sales_overview", region="East")'
            )
        if not filters:
            raise ValueError(
                f"superset.SetFilters({name!r}) sets no filters. Pass them "
                'as keywords named after the dashboard\'s filters, e.g. '
                'superset.SetFilters("sales_overview", region="East").'
            )
        super().__init__(id=id or f"filter_{str(name).strip()}")
        self.name = str(name).strip()
        # Which rendering of the dashboard to address. None means every
        # one of them.
        self.panel = (panel or "").strip() or None
        self.connection = connection
        # Keep it out of the chain UI. On a control panel the step is
        # noise — the person is filtering, not watching a workflow run —
        # and the effect is visible on the dashboard anyway. A failure
        # still surfaces, or it would have nowhere to be reported.
        self.hidden = hidden
        self.filters = dict(filters)


def build_filter_directive(
    name: str, filters: dict, panel: Optional[str] = None
) -> dict:
    """The payload a dashboard block acts on.

    `token` makes this idempotent client-side: a block applies a
    directive once per token it has not seen, so re-polling the same
    chain state does not re-apply it and fight the viewer for control of
    the filter bar.
    """
    return {
        "dashboard": name,
        # None reaches every rendering of this dashboard; a block id
        # reaches only that one.
        "panel": panel,
        # Filters keyed by the name they carry in Superset. Resolving
        # those names to filter ids happens in the browser, from the
        # embed config — which keeps this operator fire-and-forget, with
        # no Superset call inside the chain.
        "filters": filters,
        "token": next_stamp(),
    }
