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
    global _last_epoch_seconds
    with _lock:
        candidate = int(time.time()) + LOOKAHEAD_SECONDS
        _last_epoch_seconds = max(candidate, _last_epoch_seconds + 1)
        stamp = datetime.fromtimestamp(
            _last_epoch_seconds, tz=timezone.utc
        ).strftime(_TIME_FORMAT)
    return f" : {stamp}"


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
