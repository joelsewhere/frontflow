"""A form with an embedded Superset dashboard block.

Exercises `displays.Dashboard` compiling into the layout tree — the
block carries only the dashboard NAME, with the embed UUID resolved at
render time.
"""

from frontflow import Button, displays, form, inputs, page


@form(form_id="test_dashboard", title="Dashboard")
def dashboard_form():

    @page
    def landing():
        region = inputs.Text(id="region", label="Region", required=True)
        return displays.Column(
            displays.Markdown("Latest numbers:"),
            displays.Dashboard("sales_overview", height=420),
            region,
            Button("Submit"),
        )

    landing()


dashboard_form()
