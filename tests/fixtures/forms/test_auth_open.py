"""Open-mode form fixture for route-auth tests (no roles)."""
from frontflow import form, node, inputs, Button


@form(form_id="auth_open_form")
def auth_open_form():

    @node
    def step1():
        x = inputs.Text(label="X")
        return x, Button("Submit")

    step1()


auth_open_form()
