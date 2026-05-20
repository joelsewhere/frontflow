"""
Demo 2 — Conference speaker submission.

Showcases form-building breadth — varied input types, conditional
layout, and a multi-node flow — with no Airflow involved.

Three nodes:

  speaker (landing) — text, date, number, and select inputs, with a
      `@displays.branch` that asks a different follow-up per session
      format, and a `When` block that appears only once a field is set.
  logistics         — composite inputs: a number range, a multi-select,
      a checkbox list, and a checkbox grid.
  agreement         — a single checkbox plus a `When`-revealed detail,
      then a workflow-level `@backend` that compiles the record from
      explicit `steps` references.
"""

from frontflow import (
    Button,
    backend,
    displays,
    form,
    inputs,
    node,
    steps,
)


@form(
    title="Speaker submission",
    description=(
        "Propose a session for the conference. The questions adapt to "
        "your answers as you go."
    ),
    workflow_id="speaker_submission",
    submission_id="{{ steps.speaker.full_name | slugify }}",
)
def speaker_submission_workflow():

    @node
    def speaker():
        full_name = inputs.Text(
            input_id="full_name",
            label="Full name",
            required=True,
            placeholder="e.g. Dana Reyes",
        )
        available_from = inputs.Date(
            label="Available from",
            required=True,
        )
        years_experience = inputs.Integer(
            label="Years speaking on this topic",
            required=True,
            placeholder="e.g. 5",
        )
        session_format = inputs.Select(
            label="Session format",
            required=True,
            options=["Talk", "Workshop", "Panel", "Lightning talk"],
        )

        # Conditional follow-up — each session format asks for a
        # different supporting detail. The if/elif/else is traced into
        # When blocks; only the matching branch shows.
        @displays.branch
        def format_detail(session_format):
            if session_format == "Talk":
                talk_length = inputs.Select(
                    label="Talk length",
                    options=["20 minutes", "40 minutes", "60 minutes"],
                    required=True,
                )
                return (talk_length,)
            elif session_format == "Workshop":
                room_setup = inputs.Text(
                    label="Room setup needed",
                    required=True,
                )
                capacity = inputs.Integer(
                    label="Maximum attendees",
                    required=True,
                )
                return (room_setup, capacity)
            elif session_format == "Panel":
                co_panelists = inputs.Text(
                    label="Proposed co-panelists",
                    required=True,
                )
                return (co_panelists,)
            else:  # Lightning talk
                return ()

        # An explicit `When` — the bio field appears only once a name
        # has been entered, with the value interpolated live.
        short_bio = inputs.TextBlock(
            label='A short bio for {{ steps.speaker.full_name }}',
        )

        submit = Button("Continue ->")

        @backend
        def save_speaker(
            full_name, available_from, years_experience, session_format
        ):
            return None

        submit >> save_speaker(
            full_name, available_from, years_experience, session_format
        )

        return displays.Column(
            displays.Markdown(
                "Start with your details. The follow-up question "
                "changes with the session format."
            ),
            full_name,
            available_from,
            years_experience,
            session_format,
            format_detail(session_format),
            displays.When(full_name.is_filled(), short_bio),
            submit,
        )

    @node
    def logistics():
        attendee_estimate = inputs.NumberRange(
            label="Expected attendees (range if unsure)",
            required=True,
        )
        topics = inputs.MultiSelect(
            label="Topic tracks",
            options=[
                "Engineering",
                "Design",
                "Product",
                "Leadership",
                "Research",
            ],
            required=True,
        )
        equipment = inputs.CheckboxList(
            label="Equipment needed",
            options=["Projector", "Microphone", "Whiteboard", "None"],
            columns=2,
        )
        availability = inputs.CheckboxGrid(
            label="Day availability by preference",
            rows=["Day 1", "Day 2", "Day 3"],
            columns=["Prefer", "Can do", "Avoid"],
            required=True,
        )
        notes = inputs.TextBlock(
            label="Anything else about logistics?",
            placeholder="Optional",
        )
        submit = Button("Continue ->")

        @backend
        def save_logistics(
            attendee_estimate, topics, equipment, availability, notes
        ):
            return None

        submit >> save_logistics(
            attendee_estimate, topics, equipment, availability, notes
        )

        return displays.Column(
            displays.Markdown(
                "Session logistics — these composite inputs cover "
                "ranges, multi-select, and grids."
            ),
            attendee_estimate,
            topics,
            equipment,
            availability,
            notes,
            submit,
        )

    @node
    def agreement():
        code_of_conduct = inputs.Checkbox(
            label="I agree to the speaker code of conduct",
            required=True,
        )
        # A `When`-revealed detail — the dietary field appears only
        # once the agreement is given.
        dietary = inputs.Text(
            label="Dietary requirements for the speaker dinner",
            placeholder="Optional",
        )
        signature = inputs.Text(
            label="Type your full name to sign",
            required=True,
        )
        submit = Button("Submit proposal")

        @backend
        def save_agreement(code_of_conduct, dietary, signature):
            return None

        submit >> save_agreement(code_of_conduct, dietary, signature)

        return displays.Column(
            displays.Markdown("Final step — agreement and signature."),
            code_of_conduct,
            displays.When(code_of_conduct.is_filled(), dietary),
            signature,
            submit,
        )

    @backend
    def compile_submission(speaker_full, logistics_full):
        # A workflow-level backend step — runs after the nodes finish.
        # Its inputs are whole-node references passed explicitly.
        return {
            "speaker_fields": sorted((speaker_full or {}).keys()),
            "logistics_fields": sorted((logistics_full or {}).keys()),
        }

    speaker() >> logistics() >> agreement() >> compile_submission(
        steps.speaker, steps.logistics
    )


speaker_submission_workflow()
