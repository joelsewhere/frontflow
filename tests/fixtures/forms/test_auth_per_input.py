"""Per-input role fixture for route-auth tests.

Two-step form: step1 is landing (open), step2 is the multi-role
node with per-input gating. Same shape as auth_gated_form so the
landing node doesn't conflict with role checks.
"""
from frontflow import Role, form, node, inputs, Button


requester = Role("requester")
approver = Role("approver")


@form(form_id="auth_per_input_form")
def auth_per_input_form():

    @node
    def step1():
        prompt = inputs.Text(label="Prompt")
        return prompt, Button("Next")

    @node(role={"write": [requester, approver], "read": [requester, approver]})
    def step2():
        summary = inputs.Text(label="Summary", role=requester)
        decision = inputs.Radio(
            label="Decision",
            options=["Yes", "No"],
            role=approver,
        )
        return summary, decision, Button("Submit")

    step1() >> step2()


auth_per_input_form()
