"""A form with a live Superset dashboard, for end-to-end verification."""

from frontflow import Button, displays, form, inputs, node, superset, workspace


@form(form_id="sales", title="Sales entry")
def sales_form():

    @node
    def entry():
        region = inputs.Select(
            id="region", label="Region", options=["North", "South", "East", "West"]
        )
        units = inputs.Integer(id="units", label="Units")
        go = Button("Submit")

        # Placement is the point: refresh right after this submits.
        go >> superset.RefreshDashboard("sales_overview")

        return displays.Column(
            displays.Markdown("### Record a sale"),
            region,
            units,
            go,
            displays.Dashboard("sales_overview", height=420),
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
                displays.Dashboard("sales_overview", min_height=560),
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
