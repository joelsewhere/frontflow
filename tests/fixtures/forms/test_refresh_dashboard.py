"""Fixture: a form whose chain refreshes a dashboard.

Two nodes, deliberately different shapes:

  * `early`  — refresh runs immediately on submit (no operator before it)
  * `gated`  — refresh runs only AFTER a mock external operator succeeds

The pair is what demonstrates the point of the design: where the author
puts `RefreshDashboard` in the `>>` chain is when the refresh happens.
"""

from frontflow import Button, airflow, displays, form, inputs, node, superset


@form(form_id="test_refresh_dashboard", title="Refresh dashboard")
def refresh_dashboard_form():

    @node
    def early():
        region = inputs.Text(id="region", label="Region")
        go = Button("Submit")

        # No operator upstream — the refresh fires as soon as the chain runs.
        go >> superset.RefreshDashboard("sales_overview", id="refresh_early")

        return displays.Column(
            displays.Dashboard("sales_overview"), region, go
        )

    @node
    def gated():
        go = Button("Run pipeline")

        wait = airflow.TaskSensor(
            connection="mock",
            dag_id="ingest",
            task_id="load",
            run_id="manual__1",
        )
        # The refresh is placed AFTER the sensor, so it must not fire
        # until that sensor reaches success.
        go >> wait >> superset.RefreshDashboard(
            "sales_overview", id="refresh_gated"
        )

        return displays.Column(displays.Dashboard("sales_overview"), go)

    early() >> gated()


refresh_dashboard_form()
