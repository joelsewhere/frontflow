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


@workspace(
    workspace_id="sales_ops",
    title="Sales operations",
    description="Record a sale beside the dashboard it feeds.",
)
def sales_ops():
    return displays.Row(
        workspace.Form("sales"),
        # Dashboard and Explore share one dock group, so Explore opens as
        # a tab beside the dashboard — and can be dragged out from there.
        workspace.Tabs(
            displays.Dashboard("sales_overview"),
            workspace.Explore(dataset="v_frontflow_submissions"),
        ),
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
