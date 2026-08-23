"""A form with a live Superset dashboard, for end-to-end verification."""

from frontflow import (
    Button,
    backend,
    displays,
    form,
    inputs,
    node,
    steps,
    superset,
    widgets,
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
# Two nodes, and the pair is the point:
#
#   load      — an ordinary node. Pulls the units actually submitted and
#               buckets them. It closes, because loading is a thing that
#               happens once.
#   controls  — `closes=False`, so it is a control panel. It shows those
#               buckets as a filterable histogram, and every Apply
#               re-runs its chain and leaves the node open. Closing it
#               would be closing the thing you are filtering with.
# Above this many distinct values the histogram stops showing one bar
# per value and starts bucketing.
MAX_EXACT_BUCKETS = 25
BUCKET_COUNT = 12


def _label(value: float):
    """A bucket label. Whole numbers stay ints so the widget's numeric
    axis reads them cleanly and SetFilters gets a plain bound."""
    return int(value) if float(value).is_integer() else round(float(value), 2)


@form(form_id="sales_filter", title="Filter the dashboard")
def sales_filter_form():

    @node
    def load():
        go = Button("Load units")

        @backend
        def units_hist():
            """Units actually submitted, bucketed for the histogram.

            Returns `{bucket_label: count}` — what the widget takes.

            Distinct values are used while there are few of them, which
            makes the bounds the user drags to EXACT: the label is the
            value, so a selection means what it says. Equal-width
            buckets only kick in once there are too many distinct values
            to read, and there the label is the bucket's left edge —
            which is why the panel widens the upper bound below.
            """
            import os

            from sqlalchemy import create_engine, text

            engine = create_engine(os.environ["DATABASE_URL"])
            with engine.connect() as conn:
                values = [
                    float(v)
                    for (v,) in conn.execute(
                        text(
                            "SELECT (form_values->>'units')::numeric "
                            "FROM v_frontflow_submissions "
                            # Every form writes into the same
                            # form_values column, so `units` means
                            # whatever the form that wrote it meant.
                            # Cast only what is genuinely a number.
                            "WHERE jsonb_typeof(form_values->'units') "
                            "      = 'number'"
                        )
                    ).all()
                ]
            engine.dispose()

            if not values:
                return {}

            distinct = sorted(set(values))
            if len(distinct) <= MAX_EXACT_BUCKETS:
                counts = {v: 0 for v in distinct}
                for v in values:
                    counts[v] += 1
                return {_label(v): n for v, n in counts.items()}

            low, high = min(values), max(values)
            width = (high - low) / BUCKET_COUNT or 1.0
            counts = {}
            for v in values:
                index = min(int((v - low) / width), BUCKET_COUNT - 1)
                counts[_label(low + index * width)] = (
                    counts.get(_label(low + index * width), 0) + 1
                )
            return counts

        @backend
        def regions():
            """Regions actually submitted, for the picker's options.

            Same guard as the units query: every form writes into one
            `form_values` column, so a key means whatever the form that
            wrote it meant. Take only genuine strings.
            """
            import os

            from sqlalchemy import create_engine, text

            engine = create_engine(os.environ["DATABASE_URL"])
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT DISTINCT form_values->>'region' AS region "
                        "FROM v_frontflow_submissions "
                        "WHERE jsonb_typeof(form_values->'region') = 'string' "
                        "  AND form_values->>'region' <> '' "
                        "ORDER BY region"
                    )
                ).all()
            engine.dispose()
            return [r.region for r in rows]

        go >> units_hist() >> regions()

        return displays.Column(
            displays.Markdown(
                "### Load\n"
                "Pull the units and regions submitted so far, then "
                "filter them."
            ),
            go,
        )

    @node(closes=False)
    def controls():
        # The histogram IS the filter: drag a range across it. The value
        # submitted is {start, end} — the labels at the bounds.
        units = widgets.DistributionFilter(
            id="units_range",
            label="Units",
            data=steps.load.units_hist,
            value_label="submissions",
        )
        region = inputs.MultiSelect(
            id="regions",
            label="Region",
            options=steps.load.regions,
            help="Leave empty for every region.",
        )
        apply = Button("Apply")

        # Two filters, two shapes, and the difference is not the values
        # — it is what Superset says each filter IS.
        #
        #   Units  — a RANGE filter, so the pair is two BOUNDS. The same
        #            pair on a value filter would mean "either of these".
        #   Region — a VALUE filter, so the list is a set of selections.
        #
        # Region is passed as a reference rather than a template because
        # a template renders to a string, and a multi-select's answer is
        # a list. The reference keeps its type.
        apply >> superset.SetFilters(
            "sales_overview",
            panel="detail",
            hidden=True,
            Units=[
                "{{ steps.controls.units_range.start }}",
                "{{ steps.controls.units_range.end }}",
            ],
            Region=steps.controls.regions,
        )

        return displays.Column(
            displays.Markdown("### Filter"),
            units,
            region,
            apply,
        )

    load() >> controls()


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
