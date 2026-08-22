"""Fixture: a PRIVATE form carrying a dashboard block.

The public-form case cannot prove the ACL gate — a public form is
reachable by anyone by design, and so is a dashboard placed on it. This
form is restricted, so it is what actually demonstrates that a
dashboard's guest token inherits the form's access rules.
"""

from frontflow import Button, displays, form, inputs, node


@form(
    form_id="test_private_dashboard",
    title="Private dashboard form",
    private=True,
)
def private_dashboard_form():
    @node
    def landing():
        name = inputs.Text(id="name", label="Name")
        return displays.Column(
            displays.Dashboard("secret_metrics"),
            name,
            Button("Submit"),
        )

    landing()


private_dashboard_form()
