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

Optional: `pip install frontflow[superset]`. Nothing here is imported at
package load, so an install without the extra is unaffected.
"""

from .client import (  # noqa: F401
    SupersetClient,
    SupersetError,
    SupersetUnreachable,
)
from .filters import Filter  # noqa: F401
from .operators import RefreshDashboard, SetFilters  # noqa: F401

__all__ = [
    "Filter",
    "RefreshDashboard",
    "SetFilters",
    "SupersetClient",
    "SupersetError",
    "SupersetUnreachable",
]
