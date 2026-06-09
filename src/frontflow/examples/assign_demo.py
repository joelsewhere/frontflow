"""
Role-based assignment with Assign — Phase 4 demo.

A two-form hiring flow:

  - `assign_demo_request` — the manager submits a hiring request,
    picks a recruiter from the dropdown, and clicks "Send to
    recruiter."
  - `assign_demo_screening` — the recruiter receives the assignment
    (a row in submission_assignment + a child submission row), fills
    in their screening notes, and submits.

What this demonstrates:

  - `Assign` operator chained from a button via `>>`
  - A `@users` picker producing user_ids that drive `Assign(to=...)`
  - `prefill=` carrying values from the parent submission into the
    child's first step
  - `@form(on_assigned=...)` — a notification hook called after the
    assignment lands. For this demo, the hook just prints to stdout;
    real installs would send Slack / email.
  - The child form uses `@node(role=recruiter)` — the recruiter
    role is what the Assign operator grants.

Phase 4 caveats:

  - The runtime auth check (`resolve_node_access`) exists but is
    NOT wired into the form-render and submit HTTP routes yet;
    until that lands, role-gated nodes don't block non-admin
    submitters at the HTTP layer. The data flow + assignment row
    + on_assigned hook all work.
  - Frontend rendering of the parent-child relationship + the
    /my-tasks inbox UI lands separately; the data is here, the
    UI views aren't.
"""

from frontflow import (
    Assign, Button, Role, displays, form, inputs, node, users, steps,
)


# Roles, shared across both forms via import.
recruiter = Role("recruiter")


# --- The child form: recruiter fills in screening notes -----------------------


@form(
    form_id="assign_demo_screening",
    title="Candidate screening",
    description=(
        "Recruiter screens a candidate. This form is normally "
        "reached via a manager's hiring request — the assignment "
        "row pre-fills who's reviewing what."
    ),
    tags=["roles", "phase-4", "demo"],
)
def screening():

    @node(role=recruiter)
    def screen():
        notes = inputs.TextBlock(
            label="Screening notes",
            help=(
                "Anything the manager should know after your "
                "first conversation."
            ),
            required=True,
        )
        recommendation = inputs.Radio(
            label="Recommendation",
            options=["Move forward", "Hold", "Decline"],
            required=True,
        )
        return displays.Column(
            displays.Markdown(
                "## Screening: {{ steps.screen.candidate }}\n"
                "\n"
                "Open role: **{{ steps.screen.role_title }}**"
            ),
            notes,
            recommendation,
            Button("Submit screening"),
        )

    @node
    def thanks():
        return displays.Column(
            displays.Markdown(
                "## Thanks!\n\n"
                "Your screening has been recorded. The manager "
                "will see it on their hiring request."
            ),
        )

    screen() >> thanks()


# --- The parent form: manager sends a request to a recruiter -----------------


@users(label="Recruiter")
def recruiter_picker(ctx):
    """Empty body → built-in resolver. Lists every active user.
    In a real install you'd filter to actual recruiters here."""
    ...


def _notify_recruiter(event: dict) -> None:
    """on_assigned hook — fires after each assignment lands.

    The event dict carries everything a real handler would need to
    deliver a notification: the parent submission's handle, the
    child submission's handle, the assignee's user id + username,
    the role identifier, and a signed-link token (Phase 5). This
    demo just prints; a production install would post to Slack,
    send an email, or both, embedding the signed link as a URL.

    Per design doc §6.3, hook failures are logged + swallowed;
    they do NOT roll back the persisted grant.
    """
    from frontflow.dsl import signed_links
    token = event.get("signed_link_token")
    link = "<no signed link>"
    if token:
        link = signed_links.build_link(
            base_url="http://localhost:8000",
            form_id=event["child_form_id"],
            submission_handle=event["child_submission_handle"],
            token=token,
        )
    print(
        "[assign-demo on_assigned] {assignee!r} assigned role "
        "{role!r} on submission {child!r} (from parent {parent!r})\n"
        "  signed link: {link}"
        .format(
            assignee=event.get("assignee_username"),
            role=event.get("role_id"),
            child=event.get("child_submission_handle"),
            parent=event.get("parent_submission_handle"),
            link=link,
        )
    )


@form(
    form_id="assign_demo_request",
    title="Hiring request",
    description=(
        "Manager opens a hiring request, picks a recruiter, and "
        "sends the candidate to them for screening."
    ),
    tags=["roles", "phase-4", "demo"],
    on_assigned=_notify_recruiter,
)
def hiring_request():

    @node
    def request():
        candidate = inputs.Text(
            label="Candidate name",
            required=True,
        )
        role_title = inputs.Text(
            label="Open role",
            required=True,
            help="What position is this candidate being considered for?",
        )
        send = Button("Send to recruiter")

        spawn_screening = Assign(
            form="assign_demo_screening",
            to=steps.request.recruiter_picker,
            role="recruiter",
            # Prefill values land on the child's landing step as
            # the initial form_values. The child's template reads
            # them via `{{ steps.<landing_node>.<name> }}` — same
            # syntax as a normal same-node reference.
            prefill={
                "candidate": steps.request.candidate,
                "role_title": steps.request.role_title,
            },
        )
        send >> spawn_screening

        return displays.Column(
            displays.Markdown(
                "## New hiring request\n\n"
                "Fill in the candidate's details and pick the "
                "recruiter who should screen them. They'll get an "
                "assignment in their inbox."
            ),
            candidate,
            role_title,
            recruiter_picker,
            send,
        )

    @node
    def confirm():
        return displays.Column(
            displays.Markdown(
                "## Sent.\n\n"
                "**{{ steps.request.candidate }}** has been "
                "assigned to a recruiter for screening. You'll see "
                "the screening result here once it's submitted."
            ),
        )

    request() >> confirm()


screening()
hiring_request()
