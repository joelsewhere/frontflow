"""Turning a dashboard *name* into something embeddable.

Workflow authors write `displays.Dashboard("sales_overview")` — a name,
never an id. This module resolves that name to a binding, creating the
dashboard in Superset the first time it is seen.

Every step degrades rather than failing the caller. A dashboard that
could not be fully provisioned still gets a persisted binding, so the
name stays stable and `repair` can finish the job once Superset is
reachable. The alternative — refusing to render a form because a BI tool
is down — would be the wrong trade.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from ..dsl import store
from ..dsl.connections import SupersetConnection
from .client import SupersetClient, SupersetError, SupersetUnreachable

logger = logging.getLogger(__name__)

# The view Superset charts read. Created by the deployment, not here —
# see the deploy/ overlay.
SUBMISSIONS_TABLE = "v_frontflow_submissions"

# The name of the frontflow database *as registered in Superset*.
SUPERSET_DATABASE_NAME = os.environ.get(
    "FRONTFLOW_SUPERSET_DATABASE", "FrontFlow"
)


def allowed_domains() -> list[str]:
    """Origins permitted to embed provisioned dashboards.

    Superset treats an **empty list as "any domain may embed"** — see
    `is_referrer_allowed = not embedded.allowed_domains` in its embedded
    view. That is a real exposure, so set `FRONTFLOW_PUBLIC_ORIGIN` to
    the origin frontflow is served from in any deployment that matters.
    """
    raw = os.environ.get("FRONTFLOW_PUBLIC_ORIGIN", "")
    return [o.strip() for o in raw.split(",") if o.strip()]


def _title_for(name: str) -> str:
    """A human-readable Superset title from a logical name."""
    return name.replace("_", " ").replace("-", " ").strip().title()


def provision_dashboard(
    name: str,
    *,
    connection: Optional[str] = None,
    title: Optional[str] = None,
) -> dict[str, Any]:
    """Create a blank dashboard for `name` and persist its binding.

    Wires it for live refresh on the way: the submissions dataset if
    Superset does not have it, a time-range native filter on
    `created_at`, and the embed configuration.
    """
    connection_name = connection or SupersetConnection.DEFAULT_NAME

    superset_dashboard_id: Optional[str] = None
    embed_uuid: Optional[str] = None
    filter_id: Optional[str] = None

    try:
        with SupersetClient(connection) as client:
            connection_name = client.connection_name
            superset_dashboard_id = client.create_dashboard(
                title or _title_for(name)
            )

            # The filter needs a dataset to target. Without one the
            # dashboard is still created and embeddable — just not
            # live-refreshable, which `repair` can fix later.
            dataset_id = client.ensure_dataset(
                SUBMISSIONS_TABLE, SUPERSET_DATABASE_NAME
            )
            if dataset_id is not None:
                filter_id = client.ensure_time_filter(
                    superset_dashboard_id, dataset_id
                )
            else:
                logger.warning(
                    "No %s dataset in Superset; dashboard %r created without "
                    "a refresh filter.",
                    SUBMISSIONS_TABLE,
                    name,
                )

            embed_uuid = client.enable_embedding(
                superset_dashboard_id, allowed_domains()
            )
    except (SupersetError, SupersetUnreachable) as exc:
        logger.warning("Could not fully provision dashboard %r: %s", name, exc)

    return store.upsert_dashboard_binding(
        name=name,
        connection_name=connection_name,
        superset_dashboard_id=superset_dashboard_id,
        embed_uuid=embed_uuid,
        filter_id=filter_id,
        auto_created=True,
    )


def resolve_dashboard(
    name: str,
    *,
    connection: Optional[str] = None,
    provision: bool = True,
) -> Optional[dict[str, Any]]:
    """The binding for `name`, provisioning it on first use.

    `provision=False` makes this a pure lookup — used where creating a
    dashboard as a side effect would be surprising, such as validating a
    workflow at build time.
    """
    binding = store.get_dashboard_binding(name)
    if binding is not None:
        return binding
    if not provision:
        return None
    return provision_dashboard(name, connection=connection)


def repair_dashboard(
    name: str, *, connection: Optional[str] = None
) -> dict[str, Any]:
    """Fill in whatever a binding is missing.

    Recovers a binding created while Superset was down, and adopts a
    dashboard whose embed config or refresh filter was removed. Unlike
    `provision_dashboard` this raises: repair is an explicit user action,
    so silence would be unhelpful.
    """
    binding = store.get_dashboard_binding(name)
    if binding is None:
        raise SupersetError(f"No dashboard binding named {name!r}.")

    superset_dashboard_id = binding["superset_dashboard_id"]
    embed_uuid = binding["embed_uuid"]
    filter_id = binding["filter_id"]

    with SupersetClient(connection or binding["connection_name"]) as client:
        if not superset_dashboard_id:
            superset_dashboard_id = client.create_dashboard(_title_for(name))

        if not filter_id:
            dataset_id = client.ensure_dataset(
                SUBMISSIONS_TABLE, SUPERSET_DATABASE_NAME
            )
            if dataset_id is not None:
                filter_id = client.ensure_time_filter(
                    superset_dashboard_id, dataset_id
                )

        if not embed_uuid:
            embed_uuid = client.enable_embedding(
                superset_dashboard_id, allowed_domains()
            )

        return store.upsert_dashboard_binding(
            name=name,
            connection_name=client.connection_name,
            superset_dashboard_id=superset_dashboard_id,
            embed_uuid=embed_uuid,
            filter_id=filter_id,
        )


def ensure_declared_filters(
    binding: dict[str, Any], specs: list[dict[str, Any]]
) -> None:
    """Make the dashboard have the filters the DSL declared.

    Runs where the dashboard is already being resolved, so a filter
    appears the first time the dashboard is opened — the same "provision
    on first use" the dashboard itself follows. Declaring a filter and
    driving it with `SetFilters` are then the same word in the same
    file, with nothing to set up by hand in between.

    Degrades rather than fails: a dashboard that renders without one of
    its filters is far better than one that does not render. The block
    already reports a directive it cannot place.
    """
    if not specs:
        return
    dashboard_id = binding.get("superset_dashboard_id")
    if not dashboard_id:
        return

    from .filters import KINDS, expression_for

    try:
        with SupersetClient(binding.get("connection_name")) as client:
            dataset_id = client.ensure_dataset(
                SUBMISSIONS_TABLE, SUPERSET_DATABASE_NAME
            )
            if dataset_id is None:
                logger.warning(
                    "No %s dataset in Superset; cannot create declared "
                    "filters for dashboard %r.",
                    SUBMISSIONS_TABLE,
                    binding.get("name"),
                )
                return

            for spec in specs:
                kind = spec.get("kind") or "value"
                if kind not in KINDS:
                    continue
                column = spec.get("column")
                if not column:
                    continue

                # A form field has nothing to point at until it is
                # extracted out of the JSONB blob into a column.
                if spec.get("field"):
                    client.ensure_calculated_column(
                        dataset_id,
                        column,
                        expression_for(spec["field"], kind),
                        KINDS[kind]["column_type"],
                    )

                client.ensure_native_filter(
                    str(dashboard_id),
                    dataset_id,
                    spec["name"],
                    column,
                    KINDS[kind]["filter_type"],
                )
    except (SupersetError, SupersetUnreachable) as exc:
        logger.warning(
            "Could not create declared filters for dashboard %r: %s",
            binding.get("name"),
            exc,
        )
