"""Fixture: a form that filters rather than a form that submits.

Three nodes, deliberately different shapes:

  * `panel`   — `closes=False`: submit runs the chain and the node stays
                open, so it can be submitted again
  * `mixed`   — `closes=False` with a button that overrides it, so the
                same node can apply repeatedly and still move on
  * `done`    — an ordinary closing node, to have somewhere to move on to
"""

from frontflow import Button, backend, displays, form, inputs, node, superset


@form(form_id="test_filter_panel", title="Filter panel")
def filter_panel_form():

    @node(closes=False)
    def panel():
        region = inputs.Text(id="region", label="Region")
        apply = Button("Apply")

        @backend
        def narrow(region: str = ""):
            """Stands in for a query that pulls entity ids."""
            return {"ids": [f"{region.strip().lower()}-1"]}

        # An operator, not just a backend — the whole point of a
        # control panel is driving something, and operators are ticked
        # by advance(), which a node that never closes never reaches.
        apply >> narrow(region) >> superset.SetFilters(
            "sales_overview",
            id="panel_filters",
            Region="{{ steps.panel.narrow.ids }}",
        )

        return displays.Column(region, apply)

    @node(closes=False)
    def mixed():
        keyword = inputs.Text(id="keyword", label="Keyword")
        # Follows the node: re-runs, stays open.
        apply = Button("Apply", id="apply")
        # Overrides it: closes the node and moves on.
        go = Button("Continue", id="go", advances=True)
        return displays.Column(keyword, apply, go)

    @node
    def done():
        finish = Button("Finish")
        return displays.Column(displays.Markdown("Done."), finish)

    panel() >> mixed() >> done()


filter_panel_form()
