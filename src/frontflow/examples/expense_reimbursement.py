"""
Demo 3 — Expense reimbursement.

Showcases the dependency cascade and branching, with no Airflow.

  - **Cross-node dependencies.** A later step's input draws its default
    from an earlier step's answer (`default=steps.<node>.<field>`).
    Editing that earlier answer re-opens the dependent step for review
    — the cascade — so the run stays consistent. Here `approval`
    depends on `details`.
  - **Branching.** A `@backend.branch` routes on the submitted amount:
    small claims skip straight to the summary, large claims are sent
    through an extra approval node, and a claim marked as a duplicate
    ends the run early with `END`.

Four nodes:

  claim (landing)  — the expense: category, amount, and a description.
  details          — confirms the category (defaulting from the claim)
                     and holds the `@backend.branch` that routes.
  approval         — only reached for large claims; its category input
                     defaults from `details`, so editing `details`
                     cascades here.
  summary          — the final screen, displaying the filed claim.
"""

from frontflow import (
    Button,
    backend,
    displays,
    form,
    inputs,
    node,
    steps,
    END,
)

# The amount above which a claim needs the extra approval node.
APPROVAL_THRESHOLD = 500


@form(
    title="Expense reimbursement",
    description=(
        "File an expense claim. The path through the form — and which "
        "steps re-open if you edit an answer — depends on what you "
        "enter."
    ),
    workflow_id="expense_reimbursement",
    submission_id="{{ steps.claim.claimant | slugify }}",
)
def expense_reimbursement_workflow():

    @node
    def claim():
        claimant = inputs.Text(
            input_id="claimant",
            label="Your name",
            required=True,
            placeholder="e.g. Dana Reyes",
        )
        category = inputs.Select(
            input_id="category",
            label="Expense category",
            required=True,
            options=["Travel", "Equipment", "Meals", "Software"],
        )
        amount = inputs.Integer(
            input_id="amount",
            label="Amount (USD)",
            required=True,
            placeholder="e.g. 240",
        )
        description = inputs.TextBlock(
            label="Description",
            required=True,
        )
        submit = Button("Continue ->")

        @backend
        def save_claim(claimant, category, amount, description):
            return None

        submit >> save_claim(claimant, category, amount, description)

        return displays.Column(
            displays.Markdown(
                "Start with the expense itself. Later steps build on "
                "these answers — edit one and the steps that depend on "
                "it re-open."
            ),
            claimant,
            category,
            amount,
            description,
            submit,
        )

    @node
    def details():
        # `category_confirm` defaults from the claim node's category —
        # a *functional* cross-node dependency. Editing the claim's
        # category re-opens this step for review (the cascade), and the
        # dependency is drawn as an edge in the workflow graph.
        category_confirm = inputs.Select(
            label="Confirm the expense category",
            required=True,
            options=["Travel", "Equipment", "Meals", "Software"],
            default=steps.claim.category,
        )
        receipt_id = inputs.Text(
            label="Receipt or invoice number",
            required=True,
        )
        incurred_on = inputs.Date(
            label="Date the ${{ steps.claim.amount }} expense was incurred",
            required=True,
        )
        is_duplicate = inputs.Checkbox(
            label="This is a duplicate of a claim already filed",
        )
        submit = Button("Continue ->")

        # The branch routes on the claim amount and the duplicate flag.
        # It reads the claim node's amount through the `steps` accessor
        # — a cross-node dependency — so editing the amount upstream
        # re-opens this decision.
        @backend.branch
        def route_claim(
            category_confirm, receipt_id, incurred_on, is_duplicate, steps
        ):
            if is_duplicate:
                return END  # terminate — nothing more to collect
            amount = steps.claim.amount or 0
            if amount >= APPROVAL_THRESHOLD:
                return "approval"  # large claim — extra approval step
            return "summary"  # small claim — skip approval

        submit >> route_claim(
            category_confirm, receipt_id, incurred_on, is_duplicate
        )

        return displays.Column(
            displays.Markdown(
                "Confirm the supporting details. Where this goes next "
                "depends on the amount you entered."
            ),
            category_confirm,
            receipt_id,
            incurred_on,
            is_duplicate,
            submit,
        )

    @node
    def approval():
        # Reached only for large claims — the branch target.
        # `confirmed_category` defaults from the `details` node's
        # category_confirm — a *functional* cross-node dependency.
        # Editing `details` re-opens this step for review (the
        # cascade), and the dependency is drawn in the workflow graph.
        confirmed_category = inputs.Select(
            label="Category being approved",
            required=True,
            options=["Travel", "Equipment", "Meals", "Software"],
            default=steps.details.category_confirm,
        )
        approver = inputs.Text(
            label="Approving manager",
            required=True,
        )
        cost_center = inputs.Text(
            label="Cost center to charge",
            required=True,
        )
        submit = Button("Approve ->")

        @backend
        def save_approval(confirmed_category, approver, cost_center):
            return None

        submit >> save_approval(confirmed_category, approver, cost_center)

        return displays.Column(
            displays.Markdown(
                "This claim is over the approval threshold. A manager "
                "approves it and assigns a cost center."
            ),
            confirmed_category,
            approver,
            cost_center,
            submit,
        )

    @node
    def summary():
        # The final screen — no button, completes on arrival. It draws
        # values from every upstream node, so editing any of them
        # cascades all the way down to this step.
        return displays.Column(
            displays.Markdown("## Claim filed"),
            displays.Markdown(
                "**{{ steps.claim.claimant }}** filed a "
                "**{{ steps.claim.category }}** expense for "
                "**${{ steps.claim.amount }}**."
            ),
            displays.Markdown(
                "_Edit any earlier answer and the steps that reference "
                "it re-open automatically._"
            ),
        )

    # Orchestration: the >> chain is the default path. The branch in
    # `details` can divert past `approval` or end the run early.
    claim() >> details() >> approval() >> summary()


expense_reimbursement_workflow()
