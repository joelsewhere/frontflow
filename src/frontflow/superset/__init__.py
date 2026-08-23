"""Superset integration.

Three surfaces, all reached from the DSL rather than configured
out-of-band:

  - `SupersetConnection` (in `dsl.connections`) — the credentialed
    endpoint, stored like any Airflow or AWS connection.
  - `displays.Dashboard("name")` — a dashboard as a layout block.
  - `superset.RefreshDashboard("name")` — a refresh as an orchestration
    operator, placed in the `>>` chain wherever the author wants it.
  - `superset.SetFilters("name", col=...)` — the same, but pointing the
    dashboard's filters at values a `@backend` computed.
  - `superset.Filter("Region", field="region")` — a native filter the
    dashboard should have, created in Superset on first use.
  - `@superset.row_filter("name")` — which rows of it a person may see,
    applied inside the query so it cannot be widened by clicking.
  - `superset.tier("group", superset.ANALYST)` — how much query language
    a group may write to reach those rows. Independent of the row
    filter, and both apply.

Optional: `pip install frontflow[superset]`. Nothing here is imported at
package load, so an install without the extra is unaffected.
"""

from .client import (  # noqa: F401
    SupersetClient,
    SupersetError,
    SupersetUnreachable,
)
from .filters import Filter  # noqa: F401
from .rls import row_filter  # noqa: F401
from .tiers import ANALYST, EXPLORER, tier  # noqa: F401
from .operators import RefreshDashboard, SetFilters  # noqa: F401

__all__ = [
    "ANALYST",
    "EXPLORER",
    "Filter",
    "row_filter",
    "tier",
    "RefreshDashboard",
    "SetFilters",
    "SupersetClient",
    "SupersetError",
    "SupersetUnreachable",
]
