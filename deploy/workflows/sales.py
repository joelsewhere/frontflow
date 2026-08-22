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


sales_ops()
