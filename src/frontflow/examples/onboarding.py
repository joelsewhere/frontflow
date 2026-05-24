"""
Multi-section page — `@page` containing multiple `@node` section nodes.

A user-facing onboarding wizard. The `setup` page holds three sections
the user works through one screen at a time: basics → preferences →
confirm. Each section is its own `@node` with its own submit; the
`@page` is the logical container that groups them.

What this demonstrates that the others don't:

  - **`@page` with multiple sections.** Compare to `quickstart` (a
    flat single-section page) and `expense_reimbursement` (multiple
    top-level `@node`s chained at the workflow level — siblings, not
    enclosed). Here the three section nodes belong to one page; on
    the workflow graph they render inside an outer page container
    that can be collapsed to a condensed summary (Airflow
    task_group style — see the structural graph view).
  - **Page-internal flow with `>>`.** Within the page, section
    order is set by `basics() >> preferences() >> confirm()` — the
    same chain syntax workflows use at the top level, scoped to the
    page.
  - **Cross-section dependencies.** `confirm` defaults from earlier
    section answers (`steps.basics.name`, `steps.preferences.theme`)
    — the cascade works across sections just as it does across
    top-level nodes.

The form has no backend or Airflow — keeps the focus on the page
structure itself.
"""

from frontflow import Button, displays, form, inputs, node, page, steps


@form(
    form_id="onboarding",
    title="Account onboarding",
    description=(
        "A three-section wizard for new accounts. The sections share "
        "a page and the user walks through them in order."
    ),
    submission_id="{{ steps.basics.email | slugify }}",
    tags=["pages", "multi-section", "cascade"],
)
def onboarding_workflow():

    @page
    def setup():
        @node
        def basics():
            name = inputs.Text(
                id="name",
                label="Your name",
                required=True,
                placeholder="e.g. Dana Reyes",
            )
            email = inputs.Text(
                id="email",
                label="Email address",
                required=True,
                placeholder="dana@example.com",
            )
            return (
                displays.Markdown(
                    "## Step 1 — basics\n\nTell us who you are."
                ),
                name,
                email,
                Button("Continue"),
            )

        @node
        def preferences():
            theme = inputs.Radio(
                id="theme",
                label="Preferred theme",
                options=["Light", "Dark", "System default"],
                required=True,
            )
            notifications = inputs.Checkbox(
                id="notifications",
                label="Send me product updates by email",
                default=True,
            )
            return (
                displays.Markdown(
                    "## Step 2 — preferences\n\nA few defaults you "
                    "can change later."
                ),
                theme,
                notifications,
                Button("Continue"),
            )

        @node
        def confirm():
            # Pre-fills from earlier sections — the cascade carries
            # across @node boundaries inside the page just like it
            # does between top-level workflow nodes.
            display_name = inputs.Text(
                id="display_name",
                label="Display name (you can edit)",
                default=steps.basics.name,
            )
            return (
                displays.Markdown(
                    "## Step 3 — confirm\n\nLast chance to tweak "
                    "your display name before we set up the account."
                ),
                display_name,
                Button("Finish setup"),
            )

        # Page-internal chain — section order inside the page.
        basics() >> preferences() >> confirm()

    @node
    def done():
        return (
            displays.Markdown(
                "## You're in.\n\n"
                "Account set up. Welcome aboard."
            ),
        )

    # Workflow-level chain — page first, then a final done node so
    # the run has a terminal screen.
    setup() >> done()


onboarding_workflow()
