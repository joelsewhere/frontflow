# Authoring a form

Walks through writing a hello-world form from scratch, then explains
each convention as it appears. Read top-to-bottom; nothing requires
prior knowledge of the package.

If you haven't installed frontflow yet, see
[`quickstart.md`](quickstart.md) first.

## Hello world

Make a directory for your forms and a `.py` file inside it:

```bash
mkdir my_forms
cd my_forms
```

Create `hello.py`:

```python
from frontflow import Button, displays, form, inputs, node

@form(title="Hello")
def hello():

    @node
    def greet():
        name = inputs.Text(label="Your name", required=True)
        return (
            displays.Markdown("## Say hi"),
            name,
            Button("Submit"),
        )

    greet()

hello()
```

Then run:

```bash
frontflow serve .
```

Open the URL printed in the terminal and you'll find the form at
`/forms/hello`.

That's a complete, working form. The rest of this page explains
what every line is doing.

## Anatomy

### 1. The `@form` decorator

```python
@form(title="Hello")
def hello():
    ...
```

`@form` declares a workflow. The decorated function is a builder —
it runs once, when the file is imported, and produces a workflow
in frontflow's registry.

The function's name (`hello`) becomes the form id, which is what
shows up in the URL: `/forms/hello`. To override it, pass `form_id=`.

Optional keyword arguments worth knowing on day one:

| Argument | What it does |
|---|---|
| `title` | The headline shown on the form's landing page. Defaults to the function name. |
| `description` | Subtitle below the title. |
| `form_id` | URL-safe identifier. Defaults to the function name. |
| `submission_id` | A Jinja template producing a human-readable id for each submission, e.g. `"{{ steps.greet.name \| slugify }}"`. Without it, submissions are addressed by an opaque handle. |
| `tags` | A list of strings shown on the form listing — what this form demonstrates ("internal", "airflow", "customer-facing", whatever vocabulary fits). |
| `private` | When `True`, the form starts in restricted visibility — only admins and explicitly-permitted users can reach it. Applied on first discovery; admins can adjust visibility later via the UI without losing this initial state. Default `False`. |
| `default_role` | Controls fallback behavior when a node has no `role=`. Unset (the default) means "open" — anyone with form access can read + write nodes without role declarations (backward compatible). Pass `None` to put the form in **strict mode**: every node must declare `role=` or compile fails. |

See [`comparisons/publish-article-without-frontflow/`](comparisons/publish-article-without-frontflow/)
for a `@form` with all of them.

### 2. The trailing call

```python
def hello():
    ...

hello()    # ← this line
```

The decorator only registers the *recipe*. The trailing `hello()`
call is what actually builds and registers the workflow. Omit it and
the form will not appear when you serve the directory.

Why two steps: the build runs Python code with side effects
(registering nodes, wiring chains). Keeping it out of the
decorator means workflow files import cleanly without running the
build — useful for tests, for tooling that just wants the metadata,
and for catching errors at an obvious place (the call site, not the
decorator).

### 3. The `@node` decorator

```python
@node
def greet():
    ...
```

A node is one screen of the form. The function builds the screen —
declaring inputs, optionally a backend hook — and returns whatever
should render. The node's name (`greet`) is its id.

A form has one or more nodes. The simplest forms have one. Chained
forms have several connected with `>>` (see [Multi-step
forms](#multi-step-forms) below).

There's also `@page` — like `@node` but with a slightly different
default layout. Use `@node` when you want a single screen with a
submit button; use `@page` when you want a landing-page feel
(typically the first screen of a form). For one-page forms, either
works.

### 4. Calling the node inside the workflow

```python
@form(title="Hello")
def hello():
    @node
    def greet():
        ...

    greet()    # ← this call adds the node to the workflow
```

Inside the `@form` builder, calling a node (e.g. `greet()`)
**instantiates and registers it** in the workflow's chain. Define
the node, then call it. The order of calls is the order in the
chain.

For one-node forms, this is just `greet()`. For multi-node forms,
it's a chain (see below).

### 5. Inputs

```python
name = inputs.Text(label="Your name", required=True)
```

`inputs.*` declares a form field. Common types:

| Type | Renders as |
|---|---|
| `inputs.Text` | Single-line text input |
| `inputs.TextBlock` | Multi-line textarea |
| `inputs.Integer` | Numeric input (whole numbers) |
| `inputs.Slider` | Range slider |
| `inputs.Email` / `inputs.URL` / `inputs.Phone` | Format-validated text inputs |
| `inputs.Radio` | Radio group (pass `options=[...]`) |
| `inputs.Select` / `inputs.MultiSelect` | Dropdown (single / multi) |
| `inputs.Checkbox` / `inputs.CheckboxList` / `inputs.CheckboxGrid` | Boolean / list / grid of checkboxes |
| `inputs.Date` / `inputs.DateRange` / `inputs.Time` | Date and time pickers |
| `inputs.Rating` | Star rating |
| `inputs.File` / `inputs.S3File` | File upload (local / S3 bucket) |

Common kwargs:

- `label` — what appears above the input
- `required` — server-side validation (default `False`)
- `placeholder` — hint text inside the empty input
- `default` — initial value, or a templated default that pulls from
  an earlier step (e.g. `default=steps.intake.full_name`)
- `id` — the field's internal id; defaults to a slug of the label

Inputs hold references that you can reuse in templated values
later on (see [Cascading values](#cascading-values)).

### 6. The return value

```python
return (
    displays.Markdown("## Say hi"),
    name,
    Button("Submit"),
)
```

A node returns the things to render, in order. Three kinds:

- **Inputs** (`name`, `email`, …) — the form fields the user fills in
- **Displays** (`displays.Markdown`, `displays.Column`, …) — static
  content like prose, headers, layout blocks
- **Buttons** — what advances the node. At least one button is
  required for the user to submit; a node may have several, each
  routing the form differently (see [Branching](#branching)).

The return value is a tuple (or a `displays.Column(...)` containing
the same items — equivalent).

## Multi-step forms

Chain nodes with `>>`:

```python
@form(title="Sign up")
def signup():

    @node
    def basics():
        name = inputs.Text(label="Name", required=True)
        email = inputs.Text(label="Email", required=True)
        return name, email, Button("Continue")

    @node
    def confirm():
        return (
            displays.Markdown("## Confirm your details"),
            displays.Markdown(
                "Name: **{{ steps.basics.name }}**\n\n"
                "Email: **{{ steps.basics.email }}**"
            ),
            Button("Finish"),
        )

    basics() >> confirm()

signup()
```

The user submits `basics`, advances to `confirm`, submits that,
and the form is complete. State is persisted across the transition.

## Cascading values

Once a node has been submitted, its values are addressable via
`steps.<node_id>.<input_id>`. Two ways to use them:

**In Jinja templates** (inside `displays.Markdown`, in `submission_id`
templates, etc.):

```python
displays.Markdown("Hello, {{ steps.basics.name }}.")
```

**As a Python value** (inside another node's builder):

```python
@node
def confirm():
    display_name = inputs.Text(
        label="Display name",
        default=steps.basics.name,    # ← pulled from the basics node
    )
    return display_name, Button("Finish")
```

If the user goes back and edits the value `display_name` was
defaulted from, frontflow re-runs the cascade automatically — the
downstream node's submission is cleared and the user is sent back
to re-submit with the new defaults.

## Branching

Forms branch by **declaring multiple possible next nodes** and
letting a routing function pick one. The routing function is a
`@backend.branch` — a Python function that runs server-side after
the user's submit and returns the id of the next node.

```python
from frontflow import END, Button, backend, displays, form, inputs, node

@form(title="Expense claim")
def expense():

    @node
    def claim():
        amount = inputs.Integer(label="Amount", required=True)
        submit = Button("Continue")

        @backend.branch
        def route(amount):
            if amount >= 1000:
                return "approval"     # large claims need approval
            return "summary"          # small claims skip ahead

        submit >> route(amount)
        return amount, submit

    @node
    def approval():
        approver = inputs.Text(label="Approver email", required=True)
        return approver, Button("Approve")

    @node
    def summary():
        return displays.Markdown("## Submitted")

    # Call each node once and reuse the returned reference — calling
    # a node twice double-registers it. The successor list on the
    # `>>` enumerates every node the branch can return.
    claim_ref = claim()
    approval_ref = approval()
    summary_ref = summary()
    claim_ref >> [approval_ref, summary_ref]
    approval_ref >> summary_ref

expense()
```

A few branching specifics:

- **Return `END`** from a `@backend.branch` to terminate the form
  immediately (no further nodes).
- **The successor list (`>> [a, b, c]`)** must include every node
  the branch can return. Frontflow checks this at workflow build
  time; an unreachable return raises a clear error.
- **Bind each node call once** — `approval_ref = approval()` — and
  reuse the reference. Calling `approval()` a second time registers
  it twice and raises `Duplicate step id`.
- **For Airflow-driven branching** — when a human reviewer's
  decision in an Airflow HITL task should route the form — see
  `airflow.HitlBranch` in the `publish_article` example.

## Adding backend logic

A node can run server-side Python between the user's submit and
the next screen — useful for normalization, calling APIs, kicking
off DAGs:

```python
from frontflow import backend

@node
def report():
    headline = inputs.Text(label="Headline", required=True)
    submit = Button("Submit")

    @backend
    def normalize(headline):
        return {"slug": headline.strip().lower().replace(" ", "-")}

    submit >> normalize(headline)

    return headline, submit
```

The `@backend` function runs after the user clicks `submit` and
before the next node renders. Its return value is available as
`steps.report.normalize.slug` downstream.

For Airflow-driven backends, see the `airflow.TriggerDag`,
`airflow.TaskSensor`, `airflow.HitlBranch`, and `airflow.XComPull`
operators — chained the same way (`submit >> normalize >> trigger >>
build >> review`). The `publish_article` and `multi_backend_pipeline`
examples cover this end-to-end.

## Roles and access

frontflow lets you gate nodes (and individual inputs) on **roles** —
named permission symbols declared in the form. Phase 1 ships the
declaration surface and runtime gate; the full assignment system
(assigning roles to users per submission) lands in later phases.

### Declaring roles

A `Role` is a Python symbol declared at module scope:

```python
from frontflow import Role, form, node, inputs, Button

requester = Role("requester")
approver = Role("approver")
```

The string is its identifier — used in URLs, audit logs, and the
permission template snapshot. Roles are referenced **by Python
identity**, not by string — share the same `Role` object across
references via import.

### Gating a node

`@node(role=...)` accepts three shapes:

```python
# Single role — write access for this role; read auto-implied.
@node(role=approver)
def approval(): ...

# List of roles — all can write.
@node(role=[approver, senior_approver])
def critical_approval(): ...

# Verb mapping — separate write and read.
@node(role={"write": approver, "read": [approver, monitor]})
def review_with_monitor(): ...
```

Users with the write role(s) can fill and submit the node. Users
with read-only access see the node's state but inputs are
disabled. Users with neither see a "pending — assigned to
&lt;role&gt;" placeholder (the node doesn't 404).

### Gating individual inputs

Pass `role=` to an input to narrow write access for that specific
field:

```python
@node(role={"write": [requester, approver], "read": [requester, approver]})
def request_form():
    summary = inputs.Text(label="Summary", role=requester)
    decision = inputs.Radio(
        label="Approver decision",
        options=["Yes", "No"],
        role=approver,
    )
    return summary, decision, Button("Submit")
```

Both `requester` and `approver` can read the node. Only the
`requester` can fill the summary field; only the `approver` can
fill the decision. **Read follows the node** — there is no
per-input read gate; if you can read the node, you see every
input's value.

### Strict mode

By default, a node without `role=` is open to anyone with form
access (backward compatible). To require explicit role gates on
every node, pass `default_role=None` on `@form`:

```python
@form(form_id="x", default_role=None)
def workflow():
    @node  # ❌ compile error — strict mode requires role=
    def step(): ...
```

### What you get today

The Phase 1 surface gives you:

- Permission declaration in code, versioned with the form (the
  compiled permission template is part of the form_version
  snapshot — historical access decisions remain auditable).
- The runtime check (`resolve_node_access`,
  `resolve_field_access`) — admins always have full access;
  non-admins are gated.
- Compile-time validation: strict mode catches missing
  declarations, duplicate role identifiers fail at compile.

Not yet available (later phases): the `Assign` operator,
per-submission user-to-role assignment, the `/my-tasks` inbox,
signed-link notifications. Until those land, non-admin users
can't be granted roles on specific submissions, so role-gated
nodes effectively only render to admins. The declaration
surface is forward-compatible — write your forms with roles
today; users get assigned to those roles in later phases without
DSL changes.

See the bundled `role_demo_expense` example for a runnable
two-role flow.

## Picker inputs (Phase 3)

Pickers are dropdown-style inputs whose options are computed
server-side. Used to produce identifiers — frontflow user_ids,
external IDs, email addresses, group_ids — which `Assign(to=...)`
(Phase 4) consumes to create per-submission role assignments.

### Four picker decorators

```python
from frontflow import users

# Frontflow user_ids — body returns list[int]
@users(label="Recruiter")
def recruiter(ctx):
    return [42, 43, 44]

# External IDs — resolved via the resolve_external_user hook (Phase 2)
@users.external(label="Reviewer", multi=True)
def reviewers(ctx):
    return [u.sis_id for u in lms.list_recruiters()]

# Email addresses — match-or-create-stub (when Assign lands)
@users.email(label="Manager")
def manager(ctx):
    return ["alice@co.com", "bob@co.com"]

# Frontflow group_ids
@users.groups(label="Team")
def team(ctx): ...   # empty body — uses built-in resolver
```

The decorator name encodes the identifier type — no separate
`identifier_kind=` kwarg. `Assign(to=...)` reads it at compile
time to validate and resolve correctly.

### Empty-body shortcut

For "just list all frontflow users / groups", leave the body
trivial:

```python
@users(label="Pick anyone")
def anyone(ctx): ...

@users.groups(label="Any team")
def any_team(ctx): ...
```

Empty body → frontflow substitutes the built-in resolver that
queries its own User / Group tables.

`@users.external` and `@users.email` have no built-in default
(they depend on customer-side systems); empty body there is a
**load-time error**.

### Resolver context

The resolver gets a `ctx` object carrying request-scoped state.
For Phase 3 the contents are minimal; cascading dropdowns and
upstream-aware filtering land with Phase 4 when there's an
assignment context to read from.

### Pickers in node bodies

A decorated picker is an Input. Use it in node bodies like any
other input:

```python
@node
def kickoff():
    project = inputs.Text(label="Project")
    return project, recruiter, Button("Submit")
```

Per-use overrides via call syntax:

```python
@node
def emergency():
    return recruiter(required=True, label="Recruiter (urgent)")
```

The override returns a derived picker; the resolver is shared.

### Multi-select

`multi=True` on any picker decorator produces a multi-select
dropdown. The resolver returns the *available options*; the
input's *value* is a list of picked identifiers.

```python
@users.external(label="Reviewers", multi=True)
def reviewers(ctx):
    return [u.sis_id for u in fetch_team()]
```

In Phase 4, `Assign(to=steps.start.reviewers)` will fan out —
one assignment per picked identifier.

## Assigning across forms (Phase 4)

`Assign` spawns a new submission of another form, grants the
picked user a role on it, and (optionally) notifies them.

### Shape

```python
from frontflow import Assign, Role, form, node, inputs, users, steps, Button

recruiter = Role("recruiter")

@users(label="Recruiter")
def recruiter_picker(ctx): ...   # or a custom resolver

@form(form_id="hiring_screening")
def screening():
    @node(role=recruiter)
    def screen():
        notes = inputs.TextBlock(label="Notes")
        return notes, Button("Submit")
    screen()
screening()

@form(form_id="hiring_request", on_assigned=notify_recruiter)
def request_form():
    @node
    def request():
        candidate = inputs.Text(label="Candidate")
        send = Button("Send to recruiter")
        spawn = Assign(
            form="hiring_screening",
            to=steps.request.recruiter_picker,
            role="recruiter",
            prefill={"notes": "Pre-filled note"},
        )
        send >> spawn
        return candidate, recruiter_picker, send
    request()
request_form()
```

When the manager submits the `request` node:

  1. The picker's value is resolved against the submission's
     freshly-submitted form values.
  2. Each picked identifier is converted to a frontflow user_id
     per the picker's `identifier_kind`:
     `@users` → direct, `@users.external` → resolve hook,
     `@users.email` → match-or-create stub user, `@users.groups`
     → expand to member users.
  3. A child submission of `hiring_screening` is created, linked
     to the parent via `parent_submission_handle`,
     `parent_assign_node_id`, `parent_assign_op_idx`. Prefill
     values are stashed so the child form can read them.
  4. A `submission_assignment` row is inserted (assignee +
     `recruiter` role + the child submission).
  5. The form's `on_assigned` hook fires with an event dict
     carrying the parent + child handles, the assignee's
     user_id + username, and the role identifier.

### Compile-time checks

The compiler rejects:

  - `Assign(form="...")` referencing a form that isn't registered.
  - `Assign(role="...")` with a role that doesn't appear in the
    child form's permission template.
  - `Assign(to=...)` referencing a non-picker input. The error
    message points the author to `@users.email` if they want to
    assign by email.
  - `Assign(to=...)` referencing a field on a different node than
    the Assign itself.

A bad Assign downgrades the offending form to a load error
without tearing down the rest of the scan — other forms keep
serving.

### The `on_assigned` hook

```python
def notify_recruiter(event):
    """event is a dict with keys: kind, parent_form_id,
    parent_submission_handle, child_form_id,
    child_submission_handle, assignee_user_id, assignee_username,
    role_id, assignment_id, signed_link_token."""
    send_slack(event["assignee_username"], event["child_submission_handle"])

@form(form_id="...", on_assigned=notify_recruiter)
def my_form():
    ...
```

Hook failures are logged and swallowed — they do NOT roll back
the grant. Send-and-forget is the right semantic for
notifications.

### Other per-form hooks (Phase 5)

`@form(...)` accepts three more lifecycle hooks alongside
`on_assigned`. Each receives an event dict; failures are logged
and swallowed.

| Hook | Fires when | Event fields |
|---|---|---|
| `on_submitted` | A submission of this form reaches a terminal success state | `kind`, `form_id`, `submission_handle`, `submission_id`, `user_id`, `error=None` |
| `on_failed` | A submission terminates in a failed state (a backend raised, a chain step errored) | Same as above with `error` set to the failing step's message |
| `on_revoked` | An assignment on this form is revoked (admin action, external system, edit cascade) | `kind`, `form_id`, `submission_handle`, `assignment_id`, `assignee_user_id`, `assignee_username`, `role_id`, `revoked_by_user_id`, `revoked_by_username`, `revoked_at` |

```python
@form(
    form_id="my_form",
    on_assigned=notify_recruiter,
    on_submitted=archive_to_drive,
    on_failed=alert_admin,
    on_revoked=notify_revocation,
)
def my_form():
    ...
```

Terminal-state hooks (`on_submitted`, `on_failed`) fire exactly
once per submission — the runtime carries a one-shot flag so
re-calling `advance()` after termination does not re-fire.

For project-wide defaults, wrap `@form` in your own module:

```python
# my_project/form.py
from frontflow import form as _ff_form

def form(**kwargs):
    """Project-wide @form wrapper with notification defaults."""
    kwargs.setdefault("on_assigned", notify_slack)
    kwargs.setdefault("on_failed", alert_admin)
    return _ff_form(**kwargs)
```

### Signed links (Phase 5)

Each `on_assigned` event carries a `signed_link_token` — a
signed envelope granting the assignee `fill` scope on the
child submission for `link_ttl_days` (default 7, capped at 90).
Build a user-facing URL with `frontflow.dsl.signed_links.build_link`:

```python
from frontflow.dsl import signed_links

def notify_recruiter(event):
    url = signed_links.build_link(
        base_url="https://forms.example.com",
        form_id=event["child_form_id"],
        submission_handle=event["child_submission_handle"],
        token=event["signed_link_token"],
    )
    send_email(
        to=event["assignee_username"],
        body=f"Please screen this candidate: {url}",
    )
```

The token is opaque to the recipient and binds to one specific
submission for one specific user. Tampering, expiry, or
mismatched handle all fail closed (verification returns None
without distinguishing the failure mode — leaking which one
gives attackers a probing oracle).

`signed_links.verify(token, submission_handle=...)` returns the
decoded payload on success, `None` on any failure. Callers
that consume signed links must additionally check that the
user still has an active assignment on the submission — the
token's integrity is necessary but not sufficient.

### Consuming a signed link (Phase 5.5)

The form-render and submit endpoints accept a `?token=<...>`
query parameter. When present:

  1. The token-aware visibility check first tries the existing
     visibility rules (public, unlisted+key, restricted+permitted
     user). If those pass, the token is ignored.
  2. Otherwise the token is verified against the request's
     submission handle. A valid token unblocks form-level access
     for this submission specifically — the bearer doesn't need
     to be in the form's ACL.
  3. The role check then uses the token's `user_id` to look up
     active assignments. If the user holds at least one active
     role on the submission, the role gate honors it. If not
     (the assignment was revoked after the token was minted),
     the request falls through to anonymous — role-gated nodes
     render pending.

A cookie session always takes precedence over a token —
the bearer of a signed link with a different user_id can't
override an authenticated session.

```
GET  /api/forms/{form_id}/submissions/{handle}/steps/{step_id}?token=<...>
POST /api/forms/{form_id}/submissions/{handle}/steps/{step_id}?token=<...>
```

### The `/api/my-tasks` inbox

Each user's active assignments are listed via:

    GET /api/my-tasks

Returns a list of `{assignment_id, submission_handle, form_id,
form_title, role_id, granted_at, submission_state}` for every
active assignment of the signed-in user, ordered by `granted_at`
desc. Revoked rows are excluded.

### Embedding `/my-tasks` in a host page (Phase 6)

A customer's portal can embed a user's task inbox in an iframe
without requiring that user to log in to frontflow separately.
The host mints an embed-scope signed token for the right user,
includes it in the iframe `src`, and frontflow serves the inbox
bound to that user.

```html
<iframe
  src="https://forms.example.com/api/embed/my-tasks?token=<embed_token>"
  ...></iframe>
```

To enable embedded /my-tasks at all, set the install-wide
allowlist:

```
FRONTFLOW_EMBED_ALLOWED_ORIGINS=https://portal.example.com
```

Comma-separated. Without it, the route returns 404 (existence
not leaked).

Mint the token on the host's backend:

```python
from frontflow.dsl import signed_links

# After the host resolves which frontflow User to embed for
# (typically by looking up their external_id in your user
# system, then querying frontflow for the matching User.id):
token = signed_links.mint_for_embed(
    user_id=frontflow_user_id,
    ttl_seconds=3600,  # 1 hour is a sensible embed lifetime
)
iframe_src = (
    f"https://forms.example.com/api/embed/my-tasks?token={token}"
)
```

Embed tokens carry `issuer="embed"` and a wildcard
`submission_handle="*"` — they authenticate the bearer's whole
inbox, not a single submission. The embed endpoint enforces
`require_issuer="embed"` so assign-operator tokens are rejected
(prevents confused-deputy use).

CSP `frame-ancestors` is set from the install-wide allowlist for
`/embed/*` and `/api/embed/*` paths. Other routes always serve
`frame-ancestors 'none'` regardless of the allowlist.

### Phase 4 caveats

  - The runtime auth check (`resolve_node_access`,
    `resolve_field_access` from Phase 1) is now wired into both
    `GET /api/forms/{form_id}/submissions/{id}/steps/{step_id}`
    and the matching submit endpoint. A non-admin user without
    the required role sees the step as **pending** (the layout
    is stripped to a placeholder so labels/options/help don't
    leak) and gets 403 on attempted submit. Per-input `role=`
    strips submitted values for fields the user can't write
    before they reach the runtime, so the persisted record
    contains only what the user was permitted to set. Open-mode
    forms (no roles) are unaffected.
  - Frontend UI for the parent-child relationship and the
    `/my-tasks` view doesn't exist yet. The data is returned by
    the endpoints; the React views are separate work.
  - Prefill values are stored in the existing `cleared_run_ids`
    JSON column under a `_assign_prefill` key. A dedicated
    column is the clean fix; v1 reuses the existing JSON bucket
    to avoid a fourth migration this phase. Read with
    `{{ steps.<node>._assign_prefill.<key> }}` in the child
    form's templates.
  - The signed-link mechanism (Phase 5 + Phase 5.5) is live —
    assignees reach their assignment via either normal login
    + the `/my-tasks` endpoint OR by following the signed link
    embedded in an `on_assigned` notification. The link
    authenticates the bearer transparently for that one
    submission, expires per `link_ttl_days`, and is invalidated
    by revocation or key rotation.

## External identity (Phase 2)

If your install has a source-of-truth user system (LMS, HR
system, SSO IdP), frontflow can map external identifiers to
its own `User` rows without replicating the directory.

### Linking a user

Set `external_id` on a `User` row (admin UI, or directly in
the database during initial import). The column is unique
when set; nullable for frontflow-only users (admins, etc.).

### The resolve hook

For installs that don't pre-populate every user, register a
hook that resolves an unknown external_id on first touch:

```python
from frontflow import resolve_external_user
from frontflow.dsl.store import User

@resolve_external_user
def resolve(external_id: str) -> User | None:
    record = my_lms.find_user(external_id)
    if record is None:
        return None
    return User(
        username=record.username,
        external_id=external_id,
        is_admin=False,
    )
```

The hook is called **once** per (request, unknown external_id)
pair; the returned User row is persisted. Subsequent requests
hit the cheap path (DB lookup by external_id).

**No hook registered** → `resolve()` returns None for unknown
external_ids. Frontflow never invents identities silently.

### External system endpoints

Customer systems can keep frontflow's user table in sync via
admin-authenticated endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /api/users/external/{external_id}` | Look up the local User by external_id. 404 if unmapped (does NOT call the resolver hook). |
| `PUT /api/users/external/{external_id}` | Update mapped attributes (`username`, `email`). The `external_id` itself is not editable. |
| `DELETE /api/users/external/{external_id}` | Mark the user inactive. Preserves the row + audit history; blocks future auth. |

Typical use: the customer's user system hits `DELETE` via a
webhook when a user is deactivated upstream. Phase 4 extends
`DELETE` to revoke active assignments; today it just sets
`is_active = false`.

### Notification preferences

Per-user opt-out for notification channels:

```
GET  /api/users/me/notification-preferences
PUT  /api/users/me/notification-preferences
GET  /api/users/{user_id}/notification-preferences  (admin)
PUT  /api/users/{user_id}/notification-preferences  (admin)
```

The dict is open-ended (`{"email": True, "slack": False}` etc.) —
frontflow stores it but does NOT enforce. When the customer
writes notification hooks (Phase 5), the handler reads
`event.assignee.notification_preferences` and decides what to
send. The in-app inbox (Phase 4) is NOT gated by this — it's
the floor.

## Conventions worth knowing

- **One workflow per file** is the default. You *can* declare more,
  but it's discouraged — file-equals-form makes the listing
  predictable and version diffs readable.
- **The trailing call is required.** `@form` defines; the function
  call after registers. Forgetting it is the #1 first-time mistake.
- **Field ids default to slugified labels.** `inputs.Text(label="Your
  name")` is referenced as `steps.<node>.your_name`. Pass `id=` to
  override.
- **Node ids default to the function name.** `@node def greet(): ...`
  is referenced as `steps.greet.*`.
- **Templated values use Jinja2.** Inside any string field that
  supports templating (Markdown content, `submission_id`,
  Airflow operator kwargs), `{{ steps.<node>.<field> }}` resolves.
  Standard Jinja filters work — `| lower`, `| upper`, `| trim`,
  `| default("...")`, and so on — plus a few custom ones
  (`| slugify`, `| timestamp_ms`). Example: `"Hello, {{ steps.basics.name | upper }}"`
  in a Markdown block displays the name in uppercase.
- **References to the current node are resolved live in the
  browser.** When a template refers to `steps.<current_node>.<field>`,
  the server leaves the `{{ ... }}` placeholder intact and the
  client substitutes the in-progress form value as the user types.
  Refs to earlier nodes resolve server-side from the submission's
  durable state.
- **Validation is server-side.** Client-side hints exist (HTML5
  `required` attribute) but the truth lives on the server. Bad
  input is re-rendered with errors; the user can't bypass validation
  by editing client state.

## Where to go next

- [`quickstart.md`](quickstart.md) — install + run a bundled example
- [`what-is-frontflow.md`](what-is-frontflow.md) — what frontflow is
  for and what it's not
- The bundled examples are the canonical reference for patterns:
    - `quickstart` — bare one-page form
    - `input_gallery` — every input type, conditional follow-ups
    - `onboarding` — multi-section `@page` with internal sections
    - `expense_reimbursement` — multi-node branching with cascade
    - `speaker_submission` — conditional inputs based on prior answers
    - `multi_backend_pipeline` — multiple `@backend` styles in one form
    - `publish_article` — Airflow + HITL + branch routing
    - `notify_release` — variables and templated operator config
