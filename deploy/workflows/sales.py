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
