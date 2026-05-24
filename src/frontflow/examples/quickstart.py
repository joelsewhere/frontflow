"""
The smallest useful frontflow form. One page, three inputs, no
backend — new users land here to see how the pieces fit together
with as little surface area as possible: the `@form` decorator, a
`@page` decorated function, a few `inputs.*`, and a `Button`.

Compare this to `input_gallery` (every input type, multiple nodes,
conditional follow-ups, a workflow-level @backend) for the wider
tour; this file is intentionally bare.
"""

from frontflow import Button, displays, form, inputs, page


@form(form_id="quickstart", title="Quick start")
def quickstart_workflow():

    @page
    def feedback():
        name = inputs.Text(
            label="Your name",
            required=True,
            placeholder="e.g. Dana Reyes",
        )
        likelihood = inputs.Radio(
            label="How likely are you to recommend us?",
            options=["Very likely", "Somewhat", "Not at all"],
            required=True,
        )
        comment = inputs.TextBlock(
            label="Anything else?",
            required=False,
            placeholder="Optional",
        )
        return (
            displays.Markdown(
                "## Tell us what you think\n\n"
                "Three quick questions — that's the whole form."
            ),
            name,
            likelihood,
            comment,
            Button("Submit"),
        )

    feedback()


quickstart_workflow()
