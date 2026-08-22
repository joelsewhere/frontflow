"""Fixture: a form whose chain points a dashboard's filters at values a
`@backend` computed.

The claim being exercised: a filter value is an ordinary template,
resolved against prior steps when the chain reaches the operator — so
what the dashboard shows can depend on what a backend decided, not only
on what was typed.
"""

from frontflow import Button, backend, displays, form, inputs, node, superset


@form(form_id="test_dashboard_filters", title="Dashboard filters")
def dashboard_filters_form():

    @node
    def entry():
        region = inputs.Text(id="region", label="Region")
        go = Button("Submit")

        @backend
        def classify(region: str = ""):
            """Decide what the dashboard should be pointed at.

            Returns something the form never asked for (`segment`), so a
            passing test cannot be explained by the value having been
            typed in."""
            return {
                "region": region.strip().title(),
                "segment": "enterprise" if region.strip() else "unknown",
            }

        go >> classify(region) >> superset.SetFilters(
            "sales_overview",
            id="point_at_region",
            Region="{{ steps.entry.classify.region }}",
            Segment="{{ steps.entry.classify.segment }}",
        )

        return displays.Column(
            displays.Dashboard("sales_overview"), region, go
        )

    @node
    def literal():
        go = Button("Pin to East")

        # No template at all — a literal must work in the same place.
        go >> superset.SetFilters(
            "sales_overview", id="pin_east", Region="East"
        )

        return displays.Column(go)

    entry() >> literal()


dashboard_filters_form()
