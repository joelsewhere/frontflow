"""Single-section page used to test that GraphPage entries are emitted
even when a page contains one node. The frontend draws the container
regardless; the test enforces the backend doesn't suppress."""

from frontflow import Button, form, inputs, node, page


@form(form_id="test_single_section", title="Single-section page")
def workflow():

    @page
    def landing():
        @node
        def basics():
            name = inputs.Text(id="name", label="Name", required=True)
            return name, Button("Submit")

        basics()

    landing()


workflow()
