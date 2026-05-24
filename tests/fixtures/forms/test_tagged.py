"""A workflow with tags, used to test the @form(tags=...) surface."""

from frontflow import Button, form, inputs, node


@form(
    form_id="test_tagged",
    title="Tagged form",
    tags=["alpha", "beta", "gamma"],
)
def tagged():
    @node
    def landing():
        name = inputs.Text(id="name", label="Name")
        return name, Button("Submit")
    landing()


tagged()
