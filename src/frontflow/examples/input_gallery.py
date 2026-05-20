"""
Input gallery — a showcase workflow exercising every standard input
type plus conditional layout. Four nodes:

  1. basics (landing) — text, radio, single checkbox, date, and a
     `@displays.branch` that reveals a follow-up per contact method.
  2. ranges (node)    — date range, number range, multi-select, the
     checkbox grid, the checkbox list, and a `When` follow-up.
  3. extras (node)    — the typed text fields (email, phone, URL), the
     time picker, the rating scale, the slider, and file uploads — a
     transient `File` plus an `S3File` revealed by a toggle. Each
     field carries `help=` text.
  4. followup (node)  — fields drawing on earlier steps via `steps`,
     including a weighted Sankey mapping whose source column is the
     regions picked back on the `ranges` node.

Purely a demo: each node @backend just collects its fields and returns
None, and a workflow-level `compile_record` step shows explicit
`steps` arguments — a field reference and a whole-node reference.
"""

from frontflow import Button, backend, displays, form, inputs, node, steps


@form(
    title="Standard input gallery",
    description=(
        "A showcase of every standard input type and conditional "
        "layout. Fill in the basics to begin."
    ),
    workflow_id="input_gallery",
    submission_id="{{ steps.basics.full_name | slugify }}",
)
def input_gallery_workflow():

    @node
    def basics():
        full_name = inputs.Text(
            label="Full name",
            required=True,
            placeholder="e.g. Dana Reyes",
        )
        contact_method = inputs.Radio(
            label="Preferred contact method",
            options=["Email", "Phone", "Mail"],
            required=True,
        )
        start_date = inputs.Date(
            label="Start date",
            required=True,
        )
        agree = inputs.Checkbox(
            label="I agree to the terms of service",
            required=True,
        )

        # Conditional follow-ups — each contact method asks for a
        # different detail. The if/elif/else is traced into When
        # blocks; only the matching one shows.
        @displays.branch
        def contact_followup(contact_method):
            if contact_method == "Email":
                email = inputs.Text(
                    label="Email address",
                    required=True,
                    placeholder="you@example.com",
                )
                return (email,)
            elif contact_method == "Phone":
                phone = inputs.Text(
                    label="Phone number",
                    required=True,
                    placeholder="(555) 010-0000",
                )
                ok_to_text = inputs.Checkbox(label="OK to send texts")
                return (phone, ok_to_text)
            else:
                mailing_address = inputs.TextBlock(
                    label="Mailing address",
                    required=True,
                )
                return (mailing_address,)

        submit = Button("Continue ->")

        @backend
        def save_basics(full_name, contact_method, start_date, agree):
            return None

        submit >> save_basics(full_name, contact_method, start_date, agree)

        return displays.Column(
            displays.Markdown(
                "Start with the basics. The follow-up question below "
                "changes with your contact method."
            ),
            full_name,
            contact_method,
            contact_followup(contact_method),
            start_date,
            agree,
            submit,
        )

    @node
    def ranges():
        reporting_window = inputs.DateRange(
            label="Reporting window",
            required=True,
        )
        budget = inputs.NumberRange(
            label="Budget range (USD)",
            required=True,
        )
        regions = inputs.MultiSelect(
            label="Regions of interest",
            options=[
                "North America",
                "South America",
                "Europe",
                "Africa",
                "Middle East",
                "Asia Pacific",
            ],
            required=True,
        )
        coverage = inputs.CheckboxGrid(
            label="Coverage by quarter",
            rows=["Product", "Marketing", "Support"],
            columns=["Q1", "Q2", "Q3", "Q4"],
            required=True,
        )
        amenities = inputs.CheckboxList(
            label="Amenities",
            options=[
                "Parking",
                "Wi-Fi",
                "Catering",
                "A/V equipment",
                "Accessibility",
                "Breakout rooms",
            ],
            columns=2,
        )
        notes = inputs.Text(
            label="Anything else to note?",
            placeholder="Optional",
        )

        # An explicit `When` — the detail field appears once notes has
        # any text, and the label interpolates what was typed. Naming
        # the current node (`steps.ranges.notes`) resolves live.
        notes_priority = inputs.Radio(
            label='How urgent is "{{ steps.ranges.notes }}"?',
            options=["Low", "Medium", "High"],
        )

        submit = Button("Continue ->")

        @backend
        def save_ranges(
            reporting_window, budget, regions, coverage, amenities,
            notes, notes_priority,
        ):
            return None

        submit >> save_ranges(
            reporting_window, budget, regions, coverage, amenities,
            notes, notes_priority,
        )

        return displays.Column(
            displays.Markdown(
                "Composite inputs — ranges, the search multi-select, "
                "the checkbox grid, and the checkbox list."
            ),
            reporting_window,
            budget,
            regions,
            coverage,
            amenities,
            notes,
            displays.When(notes.is_filled(), notes_priority),
            submit,
        )

    @node
    def extras():
        # The inputs added in the V1 input expansion — typed text
        # fields, the time picker, the rating scale, and the slider.
        # Every field also carries `help=` text.
        work_email = inputs.Email(
            label="Work email",
            required=True,
            help="We'll only use this to send your confirmation.",
        )
        mobile = inputs.Phone(
            label="Mobile number",
            help="Optional — for time-sensitive updates only.",
        )
        portfolio = inputs.URL(
            label="Portfolio or website",
            help="Include the full https:// address.",
        )
        preferred_time = inputs.Time(
            label="Preferred call time",
            min="09:00",
            max="17:00",
            help="Business hours, your local time.",
        )
        satisfaction = inputs.Rating(
            label="How was your experience so far?",
            max=5,
            required=True,
            help="1 star is poor, 5 is excellent.",
        )
        budget_estimate = inputs.Slider(
            label="Rough budget estimate (USD)",
            min=0,
            max=10000,
            step=500,
            default=2500,
            help="Slide to your best estimate — it isn't binding.",
        )
        attachment = inputs.File(
            label="Supporting document",
            accept=["pdf", "csv", "txt"],
            max_size_mb=10,
            help="Optional — a transient upload, read but not stored.",
        )

        # S3File needs an AWS connection configured on this instance.
        # Rather than show a field that would fail on the zero-config
        # demo, a toggle gates it: leave it "No" and the S3 upload
        # stays hidden; flip to "Yes" (only if you've set up an AWS
        # connection) to try it. A live demo of conditional layout.
        s3_ready = inputs.Radio(
            label="Is S3 storage configured on this instance?",
            options=["No", "Yes"],
            default="No",
            help=(
                "Set up an AWS connection on the Connections page "
                "first — otherwise leave this on No."
            ),
        )
        receipt = inputs.S3File(
            label="Receipt (stored to S3)",
            bucket="frontflow-demo-receipts",
            key="receipts/{{ steps.basics.full_name | slugify }}/{filename}",
            accept=["pdf", "png", "jpg"],
            max_size_mb=10,
            help=(
                "Optional — persisted to S3 at "
                "s3://frontflow-demo-receipts/receipts/<your-name>/"
                "<filename>. Replace the bucket in the demo source "
                "before flipping the toggle on a real install."
            ),
        )

        submit = Button("Continue ->")

        @backend
        def save_extras(
            work_email, mobile, portfolio, preferred_time,
            satisfaction, budget_estimate, attachment, receipt,
        ):
            return None

        submit >> save_extras(
            work_email, mobile, portfolio, preferred_time,
            satisfaction, budget_estimate, attachment, receipt,
        )

        return displays.Column(
            displays.Markdown(
                "Typed text fields, a time picker, a rating scale, "
                "a slider, and file uploads — each with help text. "
                "The S3 upload below is revealed by a toggle."
            ),
            work_email,
            mobile,
            portfolio,
            preferred_time,
            satisfaction,
            budget_estimate,
            attachment,
            s3_ready,
            displays.When(s3_ready.equals("Yes"), receipt),
            submit,
        )

    @node
    def followup():
        # `steps` pulls a value from an earlier node. The primary
        # region's choices are exactly the regions picked in `ranges`;
        # the name is pre-filled from what was entered in `basics`, and
        # the label echoes that same upstream value via a template.
        confirm_name = inputs.Text(
            label="Confirm the name we'll use — {{ steps.basics.full_name }}",
            required=True,
            default=steps.basics.full_name,
        )
        primary_region = inputs.Select(
            label="Primary region",
            options=steps.ranges.regions,
            required=True,
        )

        # A Sankey mapping — column A is resolved from an earlier
        # step (the regions picked in `ranges`), column B is a fixed
        # list. `normalize` means each region's weights total 100%.
        allocation = inputs.Sankey(
            label="Allocate each region's effort across quarters",
            column_a=steps.ranges.regions,
            column_b=["Q1", "Q2", "Q3", "Q4"],
            normalize=True,
            help=(
                "Click a region, then a quarter, to connect them — "
                "each region's weights should total 100%."
            ),
        )

        # A cross-node branch — the magic `steps` parameter reaches an
        # upstream node's value, so the follow-up depends on the contact
        # method chosen back on the first screen.
        @displays.branch
        def channel_check(steps):
            if steps.basics.contact_method == "Email":
                email_current = inputs.Checkbox(
                    label="My email address is still current"
                )
                return (email_current,)
            elif steps.basics.contact_method == "Phone":
                phone_current = inputs.Checkbox(
                    label="My phone number is still current"
                )
                return (phone_current,)
            elif steps.basics.contact_method == "Mail":
                address_current = inputs.Checkbox(
                    label="My mailing address is still current"
                )
                return (address_current,)

        submit = Button("Finish")
        # A link button with a templated URL. `steps.followup.*` names
        # the current node, so the URL updates live as the region is
        # chosen — the substituted value is percent-encoded.
        region_ref = Button(
            "Region reference",
            url="https://en.wikipedia.org/wiki/{{ steps.followup.primary_region }}",
            variant="secondary",
        )

        @backend
        def save_followup(confirm_name, primary_region, allocation):
            return None

        submit >> save_followup(
            confirm_name, primary_region, allocation
        )

        return displays.Column(
            displays.Markdown(
                "These fields draw on earlier steps — the region list "
                "is whatever you selected a moment ago."
            ),
            confirm_name,
            primary_region,
            allocation,
            channel_check(),
            displays.Row(region_ref, submit),
        )

    @backend
    def compile_record(reporting_window, full_basics):
        # A workflow-level backend step — runs automatically after the
        # nodes finish. Its inputs are passed explicitly: a field
        # reference (the reporting window) and a whole-node reference
        # (everything captured in `basics`).
        return {
            "window": reporting_window,
            "basics_fields": sorted((full_basics or {}).keys()),
        }

    basics() >> ranges() >> extras() >> followup() >> compile_record(
        steps.ranges.reporting_window, steps.basics
    )


input_gallery_workflow()
