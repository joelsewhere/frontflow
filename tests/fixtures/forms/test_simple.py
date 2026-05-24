"""Minimal one-node form used by tests. Two text inputs, no
backend — exercises the create / advance / list / show endpoints
without dragging in any operators or branches."""

from frontflow import Button, form, inputs, page


@form(form_id="test_simple", title="Simple")
def simple():

    @page
    def landing():
        name = inputs.Text(id="name", label="Name", required=True)
        note = inputs.Text(id="note", label="Note")
        return name, note, Button("Submit")

    landing()


simple()
