"""Role-gated form fixture for route-auth tests.

Two-step form: step1 is landing (open), step2 is role-gated to
'approver'. This shape lets tests reach the GATED step via the
read endpoint and exercise the auth check on a step that isn't
the landing.
"""
from frontflow import Role, form, node, inputs, Button


approver = Role("approver")


@form(form_id="auth_gated_form")
def auth_gated_form():

    @node
    def step1():
        prompt = inputs.Text(label="Prompt")
        return prompt, Button("Next")

    @node(role=approver)
    def step2():
        decision = inputs.Radio(
            label="Decision", options=["Yes", "No"],
        )
        return decision, Button("Submit")

    step1() >> step2()


auth_gated_form()
