"""Two-section page used to test the GraphPage emission — a single
`@page` containing two `@node` sections. Exercises the multi-member
case (the visual reason pages became outer containers)."""

from frontflow import Button, form, inputs, node, page


@form(form_id="test_multi_section", title="Multi-section page")
def workflow():

    @page
    def intake():
        @node
        def basics():
            name = inputs.Text(id="name", label="Name", required=True)
            return name, Button("Next")

        @node
        def details():
            detail = inputs.Text(id="detail", label="Detail")
            return detail, Button("Submit")

        basics() >> details()

    intake()


workflow()
