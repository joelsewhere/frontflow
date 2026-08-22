"""Fixtures: a public and a private workspace.

The pair is what proves the access model — a public workspace serves
anyone, a restricted one does not, and the restricted case is what
authorizes (or refuses) its dashboard panel.
"""

from frontflow import Button, displays, form, inputs, node, workspace


@form(form_id="ws_entry", title="Workspace entry")
def ws_entry_form():
    @node
    def entry():
        region = inputs.Text(id="region", label="Region")
        return region, Button("Submit")

    entry()


ws_entry_form()


@workspace(
    workspace_id="ops_public",
    title="Ops (public)",
    description="A form beside a dashboard.",
)
def ops_public():
    return displays.Row(
        workspace.Form("ws_entry"),
        displays.Dashboard("ops_metrics"),
    )


ops_public()


@workspace(
    workspace_id="ops_private",
    title="Ops (restricted)",
    private=True,
)
def ops_private():
    return displays.Column(
        workspace.Form("ws_entry"),
        displays.Dashboard("secret_ops_metrics"),
    )


ops_private()
