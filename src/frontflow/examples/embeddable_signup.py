"""
Embeddable newsletter signup — a public form designed to live inside
an iframe on the company's marketing site.

What this demonstrates:

  - **`iframe_allowed_origins`.** The form declares which origins are
    allowed to embed it via the `@form` decorator. The browser
    enforces it via a `Content-Security-Policy: frame-ancestors`
    header frontflow emits on every render of this form.
  - **Subdomain glob.** `https://*.company.com` matches any subdomain
    of `company.com` at any depth — `www.company.com`,
    `blog.company.com`, `docs.staging.company.com`. The bare
    `https://company.com` is included separately because the glob
    does NOT match the bare domain.
  - **Chrome-free rendering.** When the form is loaded inside an
    iframe, the SPA detects the embedded context and reflects it
    on the document (`<body data-embedded="true">`). v1 ships
    detection; per-surface chrome-hiding hooks into the same
    mechanism as future iterations land.

To embed this form, drop an iframe pointing at the live-form route:

    <iframe
        src="https://forms.company.com/forms/embeddable_signup/form"
        width="100%" height="320" frameborder="0"
    ></iframe>

The form-filling route is `/forms/<form_id>/form` — note the
trailing `/form`. The admin route `/forms/<form_id>` is NEVER
iframable, only the live-form path.

Production note: pin `iframe_allowed_origins` to specific domains.
`["*"]` allows ANY origin to embed the form, which is fine for
genuinely public marketing forms but disables the main protection
against UI redress / clickjacking elsewhere.
"""

from frontflow import Button, displays, form, inputs, node


@form(
    form_id="embeddable_signup",
    title="Subscribe",
    description=(
        "Stay in the loop. Designed to live in an iframe on the "
        "marketing site."
    ),
    tags=["iframe", "embed", "public-facing"],
    iframe_allowed_origins=[
        # Replace with your own domains for a real install. The
        # patterns below match the company's site at any subdomain
        # — `www.company.com`, `blog.company.com`, etc. — plus the
        # bare domain (which the glob doesn't include implicitly).
        "https://company.com",
        "https://*.company.com",
        # Local dev. Useful while wiring the iframe into the host
        # page — strip in production.
        "http://localhost:3000",
        "http://localhost:5173",
    ],
)
def embeddable_signup_workflow():

    @node
    def signup():
        email = inputs.Email(
            label="Your email",
            required=True,
            placeholder="you@example.com",
        )
        cadence = inputs.Radio(
            label="How often?",
            options=["Weekly", "Monthly", "Major releases only"],
            default="Monthly",
            required=True,
        )

        return displays.Column(
            email,
            cadence,
            Button("Subscribe"),
        )

    @node
    def thanks():
        return displays.Column(
            displays.Markdown(
                "## You're in.\n\n"
                "We'll send you "
                "**{{ steps.signup.cadence | lower }}** updates at "
                "**{{ steps.signup.email }}**. Unsubscribe anytime."
            ),
        )

    signup() >> thanks()


embeddable_signup_workflow()
