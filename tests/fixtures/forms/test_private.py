"""Fixture: a form with private=True, for visibility tests."""

from frontflow import Button, form, inputs, node


@form(form_id="test_private", title="Private form", private=True)
def private_form():
    @node
    def landing():
        name = inputs.Text(id="name", label="Name")
        return name, Button("Submit")
    landing()


private_form()
