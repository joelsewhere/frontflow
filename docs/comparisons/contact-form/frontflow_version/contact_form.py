"""A simple two-node contact form with a conditional follow-up.

The user enters their name, a message, and picks how they'd like to
be contacted (email, phone, or mail). The follow-up field — what
contact detail to capture — changes based on the radio choice. A
confirmation page closes the form.

What this demonstrates:

  - **Conditional inputs.** `@displays.branch` reads the radio choice
    and reveals one of three follow-up fields without round-tripping
    to the server. Each branch returns a different tuple of inputs;
    only the matching branch renders.
  - **Two-node chain.** `intake() >> thanks()` is the smallest
    multi-step shape — submit advances to the confirmation page.
  - **Cascading values into a Markdown display.** The thanks page
    interpolates `{{ steps.intake.full_name }}` so the user sees a
    personalized confirmation without any backend code.
"""

from frontflow import Button, backend, displays, form, inputs, node


@form(
    form_id="contact_form",
    title="Get in touch",
    description="Send us a message and we'll get back to you.",
    submission_id="{{ steps.intake.full_name | slugify }}",
)
def contact_form_workflow():

    @node
    def intake():
        full_name = inputs.Text(
            label="Your name",
            required=True,
            placeholder="e.g. Dana Reyes",
        )
        message = inputs.TextBlock(
            label="Message",
            required=True,
            placeholder="What can we help with?",
        )
        contact_method = inputs.Radio(
            label="How should we reach you?",
            options=["Email", "Phone", "Mail"],
            required=True,
        )

        # Conditional reveal — each choice asks for a different
        # detail. Only the matching branch renders; the others are
        # hidden client-side as the radio changes.
        @displays.branch
        def contact_details(contact_method):
            if contact_method == "Email":
                email = inputs.Email(
                    label="Email address",
                    required=True,
                    placeholder="you@example.com",
                )
                return (email,)
            elif contact_method == "Phone":
                phone = inputs.Phone(
                    label="Phone number",
                    required=True,
                    placeholder="(555) 010-0000",
                )
                ok_to_text = inputs.Checkbox(label="OK to send texts")
                return (phone, ok_to_text)
            else:  # Mail
                mailing_address = inputs.TextBlock(
                    label="Mailing address",
                    required=True,
                )
                return (mailing_address,)

        submit = Button("Send message")

        return displays.Column(
            full_name,
            message,
            contact_method,
            contact_details(contact_method),
            submit,
        )

    @node
    def thanks():
        return displays.Column(
            displays.Markdown(
                "## Thanks, {{ steps.intake.full_name }}.\n\n"
                "Your message is in. We'll get back to you via "
                "**{{ steps.intake.contact_method | lower }}** within "
                "two business days."
            ),
        )

    intake() >> thanks()


contact_form_workflow()
