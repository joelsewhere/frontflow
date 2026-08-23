"""A form with a live Superset dashboard, for end-to-end verification."""

from frontflow import (
    Button,
    backend,
    displays,
    form,
    inputs,
    node,
    superset,
    workspace,
)


@form(form_id="sales", title="Sales entry")
def sales_form():

    @node
    def entry():
        region = inputs.Select(
            id="region", label="Region", options=["North", "South", "East", "West"]
        )
        units = inputs.Integer(id="units", label="Units")
        go = Button("Submit")

        @backend
        def focus(region: str = "", units: int = 0):
            """Decide what the dashboard should be pointed at.

            `size` is not a form field — it exists only to show that a
            filter value can come from something the backend worked out
            rather than something the person typed.
            """
            return {
                "region": region,
                "size": "large" if int(units or 0) >= 100 else "standard",
            }

        # Placement is the point. The chain computes, then points the
        # dashboard at what it computed, then refreshes.
        #
        # The refresh is still needed: SetFilters only moves the query
        # cache key when a filter VALUE changes, so two sales in the same
        # region would otherwise show stale numbers. The refresh's time
        # bound always advances.
        go >> focus(region, units) >> superset.SetFilters(
            "sales_overview",
            # Only the panel below the fold follows the submission. The
            # one beside Explore is left alone, so the whole picture and
            # the just-submitted slice can be read side by side.
            panel="detail",
            Region="{{ steps.entry.focus.region }}",
        ) >> superset.RefreshDashboard("sales_overview")

        # No dashboard in the form itself. The chain still drives one —
        # `displays.Dashboard` and the operators are independent, so a
        # chain may point a dashboard that this node does not show.
        # `sales_ops` is where it is rendered.
        return displays.Column(
            displays.Markdown("### Record a sale"),
            region,
            units,
            go,
        )

    entry()


sales_form()


# A form that FILTERS, rather than one that submits.
#
# `closes=False` makes the node a control panel: each submit runs the
# chain — the backend below, then SetFilters — and leaves the node open,
# so it can be submitted again. Nothing advances and no downstream step
# appears. Recording a sale closes, because that is a thing that
# happened; filtering a dashboard must not, because closing is what
# would stop you filtering again.
@form(form_id="sales_filter", title="Filter the dashboard")
def sales_filter_form():

    @node(closes=False)
    def controls():
        low = inputs.Integer(id="low", label="Units from")
        high = inputs.Integer(id="high", label="Units to")
        apply = Button("Apply")

        @backend
        def units_window(low=None, high=None):
            """Read the units actually submitted, and bound the request
            by them.

            The panel asks for a window; this decides what window the
            dashboard is actually given. An empty box means "as far as
            the data goes" rather than zero, and a window wider than the
            data is clamped, so the histogram never renders mostly empty
            axis.
            """
            import os

            from sqlalchemy import create_engine, text

            engine = create_engine(os.environ["DATABASE_URL"])
            with engine.connect() as conn:
                observed = conn.execute(
                    text(
                        "SELECT min((form_values->>'units')::numeric), "
                        "       max((form_values->>'units')::numeric) "
                        "FROM v_frontflow_submissions "
                        "WHERE form_values ? 'units'"
                    )
                ).one()
            engine.dispose()

            floor, ceiling = observed
            if floor is None:
                # Nothing submitted yet. Say so rather than inventing a
                # window — SetFilters drops a filter whose value does
                # not resolve, which leaves the dashboard unfiltered.
                return {"low": "", "high": "", "observed": "no submissions yet"}

            lo = float(low) if low not in (None, "") else float(floor)
            hi = float(high) if high not in (None, "") else float(ceiling)
            lo = max(lo, float(floor))
            hi = min(hi, float(ceiling))
            if lo > hi:
                lo, hi = float(floor), float(ceiling)

            return {
                "low": int(lo),
                "high": int(hi),
                "observed": f"{int(floor)} to {int(ceiling)}",
            }

        # `Units` is a RANGE filter in Superset, so the pair below is two
        # bounds rather than two selections. The filter's own type
        # decides that — the same pair on a value filter would mean
        # "either of these".
        apply >> units_window(low, high) >> superset.SetFilters(
            "sales_overview",
            panel="detail",
            hidden=True,
            Units=[
                "{{ steps.controls.units_window.low }}",
                "{{ steps.controls.units_window.high }}",
            ],
        )

        return displays.Column(
            displays.Markdown(
                "### Filter by units\n"
                "Leave a box empty for the full range."
            ),
            displays.Row(low, high),
            apply,
        )

    controls()


sales_filter_form()


# `scroll=True` lets this workspace be taller than the window. Panels
# declaring `min_height` grow the canvas until each one fits, and the
# workspace scrolls to reach what is past the fold. Without it the grid
# fills the window exactly, as a dock normally does.
@workspace(
    workspace_id="sales_ops",
    title="Sales operations",
    description="Record a sale beside the dashboard it feeds.",
    scroll=True,
)
def sales_ops():
    return displays.Column(
        displays.Row(
            # fit="content" shows the whole form rather than a sliver of
            # it: the panel takes exactly the height the form turns out
            # to need, and its vertical sash locks so it cannot be
            # dragged shorter. Width is still yours to set, and it still
            # collapses.
            workspace.Form("sales", fit="content"),
            workspace.Form("sales_filter", fit="content"),
            # Dashboard and Explore share one dock group, so Explore opens
            # as a tab beside the dashboard — and can be dragged out from
            # there. Tabs share one band of height, so the taller of the
            # two is what this row needs.
            workspace.Tabs(
                displays.Dashboard(
                    "sales_overview",
                    min_height=560,
                    show_filters=True,
                    filters_expanded=True,
                ),
                workspace.Explore(dataset="v_frontflow_submissions"),
            ),
        ),
        # Below the fold on most screens, so the workspace scrolls to
        # reach it.
        displays.Dashboard("sales_overview", min_height=520, id="detail"),
    )


# Navigation is declared once and serves every workspace in this source
# tree. Any one of them can substitute its own with
# `@workspace(nav=...)`, or opt out entirely with `nav=None`.
#
# A nav is an ordinary dock panel: it holds the same display blocks a
# form node does, and it collapses, resizes, and re-docks like any
# other. It opens collapsed to the handle below, so a workspace shows
# its panels first and its navigation only when asked.
@workspace.navigation
def main_nav():
    return workspace.Nav(
        displays.Markdown(
            "### Sales\n"
            "- [Sales operations](/workspaces/sales_ops)\n"
            "\n"
            "### Forms\n"
            "- [Record a sale](/forms/sales/form)\n"
        ),
        handle=workspace.Handle(icon="\u2630", label="Menu"),
    )


sales_ops()
