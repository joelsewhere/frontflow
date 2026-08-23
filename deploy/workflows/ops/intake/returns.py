"""A form in a nested folder, so the index has a tree to show.

Its folder comes from where this file lives — `ops/intake` — the same
rule every form and workspace follows. Nothing declares it.
"""

from frontflow import Button, displays, form, inputs, node


@form(form_id="returns", title="Record a return")
def returns_form():

    @node
    def entry():
        reason = inputs.Select(
            id="reason",
            label="Reason",
            options=["Damaged", "Wrong item", "Changed mind"],
        )
        units = inputs.Integer(id="units", label="Units returned")
        go = Button("Submit")

        return displays.Column(
            displays.Markdown("### Record a return"),
            reason,
            units,
            go,
        )

    entry()


returns_form()
