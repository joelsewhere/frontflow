"""Fixture: a form with iframe_allowed_origins set, for embedding tests."""

from frontflow import Button, form, inputs, node


@form(
    form_id="test_iframable",
    title="Iframable form",
    iframe_allowed_origins=[
        "https://company.com",
        "https://*.company.com",
    ],
)
def iframable():
    @node
    def landing():
        email = inputs.Text(id="email", label="Email")
        return email, Button("Submit")
    landing()


iframable()
