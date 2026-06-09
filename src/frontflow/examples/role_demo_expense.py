"""
Role-based access — a demonstration of the Phase 1 surface.

This form models a two-step expense approval flow with two roles:

  - `requester` (default for the first node) — anyone with form
    access can fill out the request.
  - `approver` — only users assigned the approver role can fill
    out the approval decision.

What this demonstrates:

  - `Role(...)` declared at module scope; referenced by Python
    identity (not by string)
  - `@node(role=requester)` and `@node(role=approver)` — the
    node-level write gate
  - Per-input `role=` (the approval-decision input uses it to
    narrow write to one specific input even on a multi-role node)
  - The form's `permission_template` is auto-assembled by the
    compiler — no separate config file
  - Backward compatibility: the form behaves like any other form
    in Phase 1 since the Assign operator + submission_assignment
    table land in later phases. Until then, role-gated nodes are
    only writable by admins.

Until the role-assignment system (Phase 3+) is in place, only
admin users can submit the approval-decision node. This example
is included as a forward-compatible reference; the *gate* is
real today (admins see normal write; non-admins see pending),
but assignment of non-admin users to a role is not yet
mechanically possible.
"""

from frontflow import Button, Role, displays, form, inputs, node


# Roles, declared once. Imported by-reference from anywhere else
# in the file. Two different Role("approver") objects in different
# modules are separate; share via import.
requester = Role("requester")
approver = Role("approver")


@form(
    form_id="role_demo_expense",
    title="Role-gated expense approval",
    description=(
        "Phase 1 demo: requester fills out the request; approver "
        "decides. Until assignment lands (Phase 3+), the approval "
        "node renders pending for non-admin users."
    ),
    tags=["roles", "phase-1"],
)
def role_demo_expense():

    @node(role=requester)
    def request():
        amount = inputs.Integer(
            label="Amount (USD)", required=True,
        )
        purpose = inputs.TextBlock(
            label="What is this for?", required=True,
        )
        return displays.Column(
            displays.Markdown(
                "## Expense request\n\n"
                "Submit this and an approver will review."
            ),
            amount,
            purpose,
            Button("Submit"),
        )

    @node(role=approver)
    def approval():
        decision = inputs.Radio(
            label="Decision",
            options=["Approve", "Reject"],
            required=True,
        )
        # Per-input role= narrows write on this specific field —
        # demonstrates the input-level surface. Same as the node
        # role here (so it's redundant); included to show the
        # syntax. A more realistic example would put two roles on
        # the node and use input-role to scope a sensitive field.
        notes = inputs.TextBlock(
            label="Notes (visible to all who can read this node)",
            role=approver,
        )
        return displays.Column(
            displays.Markdown(
                "## Approval decision\n\n"
                "Request amount: **{{ steps.request.amount }}**\n\n"
                "Purpose: {{ steps.request.purpose }}"
            ),
            decision,
            notes,
            Button("Submit decision"),
        )

    @node
    def thanks():
        return displays.Column(
            displays.Markdown(
                "## Done.\n\n"
                "Decision: **{{ steps.approval.decision }}**"
            ),
        )

    request() >> approval() >> thanks()


role_demo_expense()
