"""Fixture form exercising widgets.RedistributionEditor.

Two nodes: the first asks for nothing (just a button); the second
shows a histogram with a small literal dataset and a redistribution
editor configured with both policies. The form is here purely to
exercise the compile pipeline + the resolution of literal `data`,
`sources`, and `destinations` props — no StepRefs.
"""

from frontflow import Button, form, node, widgets


@form(
    form_id="test_redistribution_editor",
    title="Redistribution editor smoke",
)
def workflow():

    @node
    def configure():
        editor = widgets.RedistributionEditor(
            id="distribute",
            data={
                "2024-W01": 10, "2024-W02": 15,
                "2024-W48": 3, "2024-W49": 5,
            },
            sources=["2024-W48", "2024-W49"],
            destinations=["2024-W01", "2024-W02"],
            # Both args are optional — relying on defaults
            # (all 5 policies + match_shape).
        )
        return editor, Button("Apply")

    configure()


workflow()
