"""Two-step form used to test chain advancement, edit cascade, and
re-pin behavior. The two nodes are explicitly chained with `>>` so
submitting the first awaits the second."""

from frontflow import Button, form, inputs, node, steps


@form(
    form_id="test_two_step",
    title="Two step",
    submission_id="{{ steps.start.name | slugify }}",
)
def two_step():

    @node
    def start():
        name = inputs.Text(id="name", label="Name", required=True)
        return name, Button("Next")

    @node
    def confirm():
        # Reads from start, so cascading is exercised.
        confirmed = inputs.Text(
            id="confirmed",
            label="Confirm",
            default=steps.start.name,
        )
        return confirmed, Button("Submit")

    # Chain: start completes, then confirm awaits.
    start() >> confirm()


two_step()
