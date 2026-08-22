# Form Builder

A form builder authored via a Python orchestration file — similar to
how an Airflow DAG file produces a webpage for that DAG. You declare a
multi-step, stateful form using a decorator-based DSL, and the system
serves it as a webpage, captures submissions, and routes users through
the chain. Airflow integration is one of several `@backend` patterns
the DSL supports, not the product itself.

End-state surfaces:
- **End-user form view** — multi-step chain UI (mostly built today).
- **Admin page** — form list, submission list, per-step data inspector
  (planned, see roadmap).
- **Distribution** — server-hosted URL + embeddable iframe (planned).

## API endpoints

All paths use full words; URL-facing ids are short and URL-safe.

```
POST /forms/{form_id}/submissions              start a new submission
GET  /submissions/{submission_id}              full submission state
POST /submissions/{submission_id}/clear        rewind state (with dry_run preview)

GET  /submissions/{submission_id}/steps/{step_id}    step schema + values
POST /submissions/{submission_id}/steps/{step_id}    submit a step
```

End-user route in the frontend: `/submissions/{submission_id}`.

### Submission ids

The workflow declares how its submission id is derived:

```python
@form(
    title="Publish an article",
    form_id="publish_article",
    submission_id="{{ steps.draft.headline | slugify }}",
)
def publish_article_workflow():
    ...
```

`submission_id` is a Jinja template referencing collected values. The
id is **minted when its source becomes available** — the moment every
`steps.*` value the template references has been submitted. Until then
the submission is a session draft: no id, addressed by an internal
`handle`, and not resumable. When `submission_id` is omitted, the id is
minted at the first submit (a uuid).

The id must be URL-safe (`[A-Za-z0-9_-]` only); the runtime errors if a
fully-resolved template produces an empty or unsafe string. Collisions
return 409.

### Jinja helpers

Beyond Jinja's defaults (`| lower`, `| upper`, etc.):

- `| slugify` — ASCII-only, lowercase, alphanumeric+`-`. Use to clean
  user input for URLs.
- `| timestamp_ms` — datetime → milliseconds-since-epoch string.
- `now()` — current UTC datetime (function, not filter). Useful for
  composing unique ids: `"{{ ... | slugify }}-{{ now() | timestamp_ms }}"`.

## Step 9c — Python DSL with layout trees (current)

Workflows are defined in a Python DSL inspired by Airflow's DAG syntax.
The DSL has **no hidden registration**: every operator — display
blocks, inputs, buttons, backend calls, external tasks — is a pure
value object. A node body constructs operators, wires the execution
graph with `>>`, and **returns its layout tree**.

Two structures, no third mechanism:

- **layout** — what the user sees. The operator tree the node body
  `return`s: containers (`Column`, `Row`, `Card`, …) nesting display
  blocks, inputs, and buttons.
- **execution** — what runs on submit. The `>>` graph, reached by
  walking downstream from the buttons in the layout tree: at most one
  `@backend` call, then a chain of external tasks.

The backend ships the layout tree as structured JSON (a `{type, id,
props, children}` tree). The frontend renders it with a recursive
component registry — no HTML over the wire.

### Anatomy of a workflow

```python
from frontflow import (form, page, node, inputs, widgets, displays,
                       Button, backend, airflow, END)

@form(
    title="Publish an article",
    description="Submit an article and we'll run it through the pipeline.",
    form_id="publish_article",
    submission_id="{{ steps.draft.headline | slugify }}",
)
def publish_article_workflow():

    @node                                        # ← first node = entry
    def draft():
        headline = inputs.Text(input_id="headline", required=True)
        submit = Button("Send to pipeline ->")

        @backend
        def kickoff(headline: str) -> dict:
            return {"headline": headline.strip()}

        trigger = airflow.TriggerDag(
            connection="prod_airflow",
            dag_id="publish_article",
            conf={"headline": "{{ steps.draft.headline }}"},
        )
        build = airflow.TaskSensor(
            connection="prod_airflow",
            dag_id="publish_article",
            task_id="build_content",
            run_id="{{ steps.trigger_publish_article.run_id }}",
        )

        # >> wires the EXECUTION graph
        submit >> kickoff(headline) >> trigger >> build

        # return is the LAYOUT tree
        return displays.Column(headline, submit)

    @node
    def review():
        @displays.table(title="Summary")
        def summary_data():
            return {"word_count": 820, "channel": "Blog"}

        comments        = inputs.TextBlock(label="Comments (optional)")
        approve         = Button("Approve")
        request_changes = Button("Request changes")
        reject          = Button("Reject")

        @backend.branch
        def submit(approve, request_changes, reject, comments):
            if reject:           return END                  # terminate
            if request_changes:  return "draft"              # loop back
            return None                                      # fall through

        [approve, request_changes, reject] >> submit(
            approve, request_changes, reject, comments
        )

        return displays.Column(
            displays.Markdown("Review the article below."),
            summary_data(),
            comments,
            displays.Row(approve, request_changes, reject),
        )

    # Orchestration: call order determines execution order.
    draft() >> review()


# Trailing call registers the workflow in WORKFLOWS so main.py can
# find it at startup. Mirrors Airflow's `my_dag = my_dag()` pattern.
publish_article_workflow()
```

A node body that returns nothing — or returns a non-operator — raises a
`ValueError`/`TypeError` at build time. A node with no `Button` in its
returned tree fails at compile time.

### Superset dashboards

`pip install frontflow[superset]` adds two things to the DSL: a
dashboard you can place in a layout, and a refresh you can place in a
chain.

```python
from frontflow import Button, displays, form, inputs, node, superset, airflow

@node
def upload():
    go = Button("Run pipeline")
    trigger = airflow.TriggerDag(connection="prod", dag_id="ingest")
    wait = airflow.TaskSensor(
        connection="prod", dag_id="ingest", task_id="load",
        run_id="{{ steps.trigger_ingest.run_id }}",
    )

    # The refresh is an orchestration step, so WHERE you put it is WHEN
    # it happens — here, only once the DAG has finished loading data.
    go >> trigger >> wait >> superset.RefreshDashboard("sales_overview")

    return displays.Column(displays.Dashboard("sales_overview"), go)
```

Placing `RefreshDashboard` earlier in the chain refreshes earlier;
leaving it off one branch means that branch never refreshes. Nothing
refreshes implicitly on submit.

### Setup

1. **Add a connection.** *Connections → Apache Superset*, named
   `superset_default` (the conventional name, like `airflow_default`).
   Its base URL is how *frontflow* reaches Superset; credentials are a
   Superset service account able to create dashboards and datasets.
   The password is Fernet-encrypted at rest.

2. **Register frontflow's database in Superset**, named `FrontFlow` (or
   set `FRONTFLOW_SUPERSET_DATABASE`). Point it at frontflow's Postgres
   through a **read-only** role — Superset should never be able to write
   your submission data.

3. **Reference a dashboard by name.** A name with no dashboard behind it
   is provisioned on first use: a blank dashboard, the
   `v_frontflow_submissions` dataset, a time-range filter on
   `created_at`, and the embed configuration. Build the charts you want
   in Superset; frontflow keeps the name pointing at it.

`deploy/` has a docker-compose overlay that stands all of this up —
Postgres with the read-only role, Superset, and frontflow — for local
use.

### What Superset reads

`v_frontflow_submissions`, a flattened view created alongside the
schema: one row per step with its submission and form context, and
`form_values` left as JSONB so per-form fields stay reachable
(`form_values->>'region'`) with no schema change when a form gains a
field. Soft-deleted submissions are excluded, matching every other read
path.

For downstream warehouses, use `GET /api/export/submissions` instead —
the view is for live dashboards, the export API for batch collection.

### Security

**`FRONTFLOW_PUBLIC_ORIGIN` restricts who may embed your dashboards, and
is unset by default.** Superset treats an empty allowed-domains list as
*any domain may embed* (`is_referrer_allowed = not
embedded.allowed_domains`), so set it to the origin frontflow is served
from before exposing anything.

A dashboard is exactly as reachable as the form it sits on. Both
dashboard endpoints are form-scoped and check two things: the form's own
visibility rules, and that the dashboard actually appears in that form.
Putting a dashboard on a **public** form therefore publishes it to
anonymous visitors — which is intended, but worth deciding deliberately.

### Version pinning

The in-place refresh uses Superset's `setDataMask`, which released
Superset does not implement. The compose overlay pins the Superset image
to a master build, and `frontend/src/vendor/superset-embedded-sdk` is
vendored from the **same commit**. The two must be bumped together; see
that directory's README.

## Connections

Each Airflow operator and `S3File` input resolves its credentials
through the connection store. Two conventions:

- **`airflow_default`** — the conventional name for the default
  Airflow connection. An operator with `connection=None` (or with the
  argument omitted) looks up this name. A missing default raises a
  clear error rather than silently mocking — wire up the connection,
  or pass `connection=` explicitly.
- **`aws_default`** — the conventional name for the default AWS
  credentials, used by `S3File`. When missing, `S3File` falls through
  to boto3's default credential chain (environment variables,
  `~/.aws/credentials`, IAM instance role, etc.) — the AWS-native
  fallback. This is the only resolver with a meaningful "missing →
  fallback" path; Airflow has no equivalent.

To dispatch an operator without any real Airflow at all — useful when
authoring or demoing a workflow before the infrastructure exists —
pass `connection="mock"`. The runtime then progresses the operator
through a deterministic mock state machine instead of calling
Airflow. Mixing mock and real operators in one node's chain is an
author error and will fail at dispatch.

### Reading data from S3

A `@backend` that needs to read from S3 uses `S3Hook` — a thin wrapper
around a credential-resolved boto3 client. `S3Hook()` looks up the
conventional `aws_default` connection from the store; a missing
connection falls through to boto3's default chain (env vars,
`~/.aws/credentials`, IAM role) — same semantics as `S3File` uploads.

```python
from frontflow.aws.hooks import S3Hook

@backend
def fetch_codes(steps):
    return S3Hook().read_json(
        bucket="my-bucket",
        key=f"runs/{steps.start.run_id}/codes.json",
    )
```

`S3Hook` exposes `read_bytes`, `read_json`, `read_csv` (returns a
pandas DataFrame; `**kwargs` forward to `pandas.read_csv`), and
`presigned_get_url` (returns a time-limited download URL —
`expires_in` defaults to one hour). pandas is a soft dependency:
`read_csv` raises a clear error when it's not installed.

**Pattern: backends feed inputs in a later node.** When a backend
fetches data that a downstream input or widget consumes, place it in
the *previous* node's chain — *after* the operators that produce its
inputs, *before* the form advances. The next node then renders with
the data already in `steps`:

```python
@node
def upload():
    submit = Button()
    trigger = airflow.TriggerDag(connection="prod", dag_id="ingest", ...)
    wait = airflow.TaskStateSensor(
        connection="prod", dag_id="ingest", task_id="extract_codes",
        run_id="{{ steps.trigger_ingest.run_id }}",
        target_state="deferred",
        waiting_message="Parsing your datasets...",
    )

    @backend
    def fetch_codes(steps):
        return S3Hook().read_json(
            bucket="my-bucket",
            key=f"runs/{steps.start.run_id}/codes.json",
        )

    # backends in the chain: run *after* the sensor succeeds, *before*
    # the form advances to `select`.
    submit >> trigger >> wait >> fetch_codes()
    return submit

@node
def select():
    # `steps.fetch_codes['return']` is already populated when this
    # node renders — the chain ran fetch_codes before advancing.
    codes = inputs.CheckboxList(
        input_id="codes",
        options=steps.fetch_codes["return"],
    )
    return codes, Button()
```

The reverse — placing the backend *inside* `select` after its submit
button — would run the backend only *after* the user submits, by which
point the input has already rendered with empty options.

### The display palette

Containers nest children; leaves are terminal. Inputs and buttons are
tree elements too.

| Block                  | Role                                              |
|------------------------|---------------------------------------------------|
| `displays.Column`      | Vertical stack (the usual root).                  |
| `displays.Row`         | Horizontal layout — children side by side.        |
| `displays.Card`        | Bordered group, optional `title`.                 |
| `displays.Section`     | Titled region.                                    |
| `displays.Callout`     | Attention box; `variant` info/warning/success/error. |
| `displays.Markdown`    | Prose; the frontend renders it (react-markdown).  |
| `displays.Divider`     | Horizontal rule.                                  |
| `displays.Image`       | Image with optional caption.                      |
| `@displays.table`      | Read-only key/value table from a function.        |
| `displays.When`        | Conditional container — children show only when its condition holds. |
| `@displays.branch`     | Decorator — `if`/`elif`/`else` over a field, compiles to `When` blocks. |
| `inputs.*`             | Form inputs — see the input catalogue below.      |
| `widgets.DistributionFilter` | Interactive range-filter histogram. `data` is a `{x_value: count}` dict (or a `StepRef` resolved at runtime). |
| `Button`               | A submit action (`>>` wires what runs after it) or, with `url=`, a link button. `variant`: primary/secondary/danger. |

#### Input catalogue

Every input's variable name is its field id by default (`input_id=`
overrides). `required` makes the field mandatory.

| Input                  | Submitted value          | Notable params              |
|------------------------|--------------------------|-----------------------------|
| `inputs.Text`          | string                   | `placeholder`, `default`    |
| `inputs.Integer`       | number                   | `placeholder`, `default`    |
| `inputs.TextBlock`     | string (multi-line)      | `placeholder`               |
| `inputs.Email`         | string                   | format-validated, email keyboard |
| `inputs.Phone`         | string                   | telephone keypad on mobile  |
| `inputs.URL`           | string                   | format-validated, URL keyboard |
| `inputs.Select`        | one option string        | `options`                   |
| `inputs.Radio`         | one option string        | `options`                   |
| `inputs.MultiSelect`   | list of option strings   | `options` — search-to-filter dropdown |
| `inputs.Checkbox`      | bool                     | required ⇒ must be ticked   |
| `inputs.Date`          | ISO date string          | `min`, `max`                |
| `inputs.Time`          | `"HH:MM"` string         | `min`, `max`                |
| `inputs.DateRange`     | `{start, end}`           | —                           |
| `inputs.NumberRange`   | `{min, max}`             | —                           |
| `inputs.Rating`        | integer 1..max           | `max` (default 5)           |
| `inputs.Slider`        | number                   | `min`, `max`, `step`        |
| `inputs.File`          | upload reference         | `accept`, `max_size_mb` — transient; bytes reach `@backend` |
| `inputs.S3File`        | S3 reference             | `key` (templatable), `bucket` — both required; `accept`, `max_size_mb` — persisted to S3 |
| `inputs.CheckboxGrid`  | `{row: [columns]}`       | `rows`, `columns` — checkbox matrix |
| `inputs.CheckboxList`  | list of option strings   | `options`, `columns` — flat checkbox grid |
| `inputs.Sankey`        | list of `{from,to,weight}` | `column_a`, `column_b`, `normalize` — weighted mapping |

Every input also accepts `help=` — a short hint shown beneath the field.

The `widgets.DistributionFilter` x-axis is a generic range filter: its keys may
be ISO dates **or** numbers (ints/floats) — the widget infers the axis
type from the data. `data` may be a literal dict, or a `StepRef`
(`steps.<backend_fn_name>['return']`) for runtime-sourced data — e.g.
a `@backend` that pulls a JSON file from S3 via `S3Hook`.
`workflows_user/input_gallery.py` is a runnable showcase of every input
and of conditional layout.

#### Conditional layout

Parts of a node can show or hide based on what the user has entered.
`displays.When(condition, *children)` is the primitive — its children
render only while the condition holds, evaluated live (client-side)
against the form's current values:

```python
displays.When(contact_method.equals("Email"), email_address)
```

Conditions are built off any input: `.equals(v)`, `.not_equals(v)`,
`.in_(values)`, `.not_in(values)`, `.is_filled()`, `.is_blank()`.

`@displays.branch` is ergonomic sugar — write ordinary `if` / `elif` /
`else` over a controlling field and it compiles down to `When` blocks:

```python
@displays.branch
def contact_followup(contact_method):
    if contact_method == "Email":
        email = inputs.Text(label="Email address", required=True)
        return (email,)
    elif contact_method == "Phone":
        phone = inputs.Text(label="Phone number", required=True)
        return (phone,)
    else:
        address = inputs.TextBlock(label="Mailing address", required=True)
        return (address,)

# place it in the layout, passing the controlling field:
return displays.Column(contact_method, contact_followup(contact_method), submit)
```

The controlling field arrives as a recording proxy: the body is traced
once per branch (path exploration) so every `if`/`elif`/`else` path
becomes its own `When` with the right cumulative condition. A required
field inside a hidden `When` doesn't block submission, and a hidden
field's value is pruned from the payload.

A branch can also condition on an *earlier node's* value. Declare a
magic `steps` parameter and drill into upstream data —
`steps.<node>.<field>`:

```python
@displays.branch
def channel_check(steps):
    if steps.intake.contact_method == "Email":
        return (email_still_current,)
    elif steps.intake.contact_method == "Phone":
        return (phone_still_current,)

# the steps parameter is injected — call it with no arguments:
return displays.Column(..., channel_check(), submit)
```

Naming a node without a field — `steps.intake` — is a *whole-node*
reference: the branch depends on everything in that node, the same
coarse dependency available to a workflow-level backend.

For an explicit `When`, the `steps` accessor builds the same
cross-node condition: `displays.When(steps.intake.method.equals("Mail"), …)`.
Cross-node conditions reference an upstream value that's already fixed
by the time the node is shown, so they're resolved server-side — a
`When` whose condition fails is dropped before the layout is served,
and the frontend only ever sees same-node conditions.

#### Templating

Any `label` or button `url` supports templating with the same
`{{ steps.<node>.<field> }}` namespace used by `submission_id` and
`run_id` — one namespace, regardless of where the template sits. You
always name the node, even the one you're in:

```python
label='How urgent is "{{ steps.ranges.notes }}"?'   # a field on this node
url="https://example.com/wiki/{{ steps.detail.primary_region }}"
```

The *syntax* is uniform; the only thing that varies is *when* a
reference resolves, and the system picks that from which node is
named:

- naming the **current node** → resolved live in the browser, updating
  as the user types;
- naming an **earlier node** → resolved to that node's submitted value
  when the screen is served (substituted values are percent-encoded
  inside a `url`).

#### Step references

A field's `options` or `default` can be drawn from an *earlier* node
instead of being fixed at definition time. `steps.<node>.<field>`
names a value — `<field>` being an input id or a `@backend` function
name in that node, the same namespace as the templates above:

```python
@node
def detail():
    # the choices are exactly the regions picked in the `survey` node
    primary_region = inputs.Select(
        label="Primary region",
        options=steps.survey.regions,
    )
    # pre-filled from what was entered upstream
    confirm_name = inputs.Text(
        label="Confirm your name",
        default=steps.intake.full_name,
    )
```

Used this way — for `options`, `default`, or a `When` condition — the
named node must be an earlier one: the value is read from submitted
data. (In a template you may also name the current node, which
resolves live.)

The reference is inert at definition time (a node may reference one
defined later in the file) and is resolved server-side each time the
node is served for a submission, against that submission's data — so
the frontend just receives a normal `options` list. `options`
references should point at list-valued sources (a `MultiSelect`,
`CheckboxList`, or a `@backend` returning a list). The named node must
be an earlier one; the compiler rejects references to the entry node,
to the node itself, or to an unknown node.

#### Tuple shorthand for layout

A node body returns its layout tree. Instead of writing `displays.Column`
and `displays.Row` explicitly, a node may return **bare tuples (or
lists)** — they expand to alternating containers: the outermost is a
Column (vertical), and every level of nesting flips the axis.

```python
return (
    (first_name, last_name),   # a Row
    submit,
)
# == displays.Column(displays.Row(first_name, last_name), submit)

return (
    ((first_name, last_name), (birthdate, address)),  # a Row of two Columns
    submit,
)
# == Column( Row( Column(first_name, last_name),
#                  Column(birthdate, address) ),
#            submit )
```

Any DSL element — an input, a `Button`, a `displays.*` block, an
explicit `displays.Column/Row/Card/...` — is used as given, so the
shorthand and explicit containers mix freely. `None` entries are
dropped, so `field if cond else None` works. The shorthand only applies
to a node's *return value*; an explicit container still takes explicit
children.

### Decorator pattern

`@form`, `@page`, and `@node` all follow the same template/call
pattern as Airflow's `@dag` and `@task`:

- **Decoration declares.** Produces a template object that's inert
  until called.
- **Calling registers.** Calling the template runs the body. The
  workflow's body, when executed, registers itself in `WORKFLOWS`;
  a page's body registers its section nodes; a node's body registers
  its operators.
- **`>>` declares execution edges.** A step runs after the steps wired
  into it with `>>` — see "Orchestration semantics" below.

Both `@page` and `@node` accept an optional `title=` for display. The
workflow's entry step is whichever page/node is registered first; no
decorator is needed to mark it.

### Pages

A workflow is a graph of **steps**: pages, top-level nodes, and backend
steps, wired with `>>`. A **page** is its own navigated view — reaching
it moves the user *into* that page, where they see only its content.

A page is written one of two ways:

```python
# Sectioned — the body declares @node sections, worked through one at a
# time, each with its own submit. The page ends when its last section
# (the one with no internal >>) is submitted.
@page
def signup():
    @node
    def account():
        email = inputs.Text(input_id="email", required=True)
        return (email, Button("Next"))
    @node
    def profile():
        name = inputs.Text(input_id="name", required=True)
        return (name, Button("Create account"))
    account() >> profile()

# Flat — the body returns a layout directly (one implicit node).
@page(title="Your details")
def details():
    size = inputs.Integer(input_id="team_size")
    return (size, Button("Finish"))

signup() >> details() >> done()      # workflow-level wiring
```

`>>` inside a page body wires its **section nodes** (a page-internal
graph); `>>` between page/node refs wires the **workflow**. A workflow
with only top-level `@node`s and no `@page` is a single-page form — one
flow from beginning to end.

Each view has its own URL —
`/forms/:formId/submissions/:submissionId/:viewId` — where `:viewId` is
the page id (or, for a run of page-less top-level steps, that run's
lead node id). The bare submission URL redirects to whichever view is
live; a single-page form keeps the bare URL. Browser back/forward and
refresh work, and as the workflow advances a user on the live view is
carried into the next page.

The page id (and node id) defaults to the decorated function's name; an
explicit `id=` overrides it, which is also the URL segment:

```python
@page(id="intake", title="Document Intake")
def intake_page():
    ...
```

### Role markers

| Decorator | Role |
| --- | --- |
| `@page` | A navigated page of section nodes (or a flat page). |
| `@node` | A single screen — a page section node, or a top-level workflow step. |

The workflow's entry is whichever page or node is registered first —
the one declared at the top of the `@form` body. It cannot have
upstream dependencies (there's nothing for them to refer to). Its
first submit creates the submission.

### Orchestration semantics

Decorating only **declares** — it doesn't register anything. **Calling**
registers a step; **`>>`** declares the execution edges between steps.
Both are required — the runtime walks the `>>` graph, so a step that
isn't wired is unreachable.

```python
# Linear chain — each >> is one edge
collect() >> review() >> done()

# Branch — a @backend.branch fans out to its declared targets
collect() >> route() >> [approve_path(), reject_path()]
```

Rules:

- A step with **no** downstream edge is **terminal** — reaching it ends
  the submission. The terminal node/page *is* the completion screen:
  there is no separate completion construct, you author it like any
  other step. A terminal node may be **buttonless** — a pure "you're
  done" screen that completes the moment it's reached. A terminal node
  *with* a button instead completes when that button is submitted (the
  "review & finish" pattern). Every non-terminal node still needs a
  Button — it's how the flow advances.
- A step with **one** downstream edge runs that step next.
- A step with **more than one** downstream edge must be a
  `@backend.branch` — only a branch may fan out. (A plain step with
  multiple downstream edges is a compile error.)
- A `@backend.branch` may only route to a step it is wired to. Returning
  an id that isn't in its downstream fails the submission with a clear
  error — workflow dependencies are always explicit.

Data dependencies are separate from execution edges: passing a node's
ref as a call argument (`b(out)` where `out = a()`) threads data, and is
independent of the `>>` graph.

For independent branches, the two paths are alternatives — each is its
own terminal (or has its own downstream). Parallel fan-out rendering is
on the roadmap (see 9h below).

### Backend steps

A `@backend` function has two roles, decided by *where it is called*:

**Inside a node body** — wired to a button with `>>` — it is a submit
action that receives operator arguments (field values, button flags):

```python
@node
def review():
    approve = Button("Approve")
    approve >> finalize(approve, comments)
    return displays.Column(comments, approve)
```

**At workflow scope** — placed directly in the `>>` chain — it is a
standalone step that runs automatically the moment the flow reaches it,
with no screen:

```python
collect_name() >> greet(steps.collect_name.full_name) >> done()
```

A workflow-level backend step has no operators in scope. The values it
needs are passed explicitly at the call site as `steps` references, so
its dependencies are visible:

```python
collect() >> notify(steps.collect.email, steps.collect.region) >> done()
```

The function takes ordinary parameters (`def notify(email, region)`);
each `steps.<node>.<field>` argument is resolved against the
submission's accumulated data and bound positionally. `@backend.branch`
works as a workflow step too — it takes its deciding values the same
way — and its return value routes exactly as it does node-attached.

An argument may also be a *whole-node* reference, `steps.<node>` with
no field — the function then receives that node's entire value dict,
and the step depends on everything in the node (any change anywhere in
it counts). It's the deliberate "redo this if anything upstream
moves":

```python
collect() >> audit(steps.collect) >> done()   # audit depends on all of collect
```

By default a backend step shows in the chain as a compact marker. Pass
`hidden=True` to suppress it — this works on `@backend` and
`@backend.branch` alike:

```python
@backend(hidden=True)
def audit_log(steps): ...

@backend.branch(hidden=True)
def route(steps): ...
```

If a backend step raises (or a branch returns an un-wired id), the
submission fails — the step is marked failed in the chain and the
submission state becomes `failed`. A `hidden=True` step is suppressed
only while it succeeds; a failure always surfaces, so it has somewhere
to show.

### The `steps` accessor

Any `@backend` function — node-attached or standalone — may declare a
parameter named `steps`. The runtime injects an accessor over every
step that has run so far:

```python
@backend
def greet(steps):
    first = steps.collect_name.first_name   # a submitted field value
    checked = steps.validate                # a backend step's return
    ...
```

- `steps.<node_id>.<input_id>` — a node's submitted field value
- `steps.<node_id>.<backend_fn>` — a node-attached backend's return
- `steps.<backend_step_id>` — a standalone backend step's return value

Reading a step or field that has not run raises `AttributeError`, so
typos surface immediately. It is the Python mirror of the `steps`
namespace already available in Jinja templates.

### Submission ids

A minted submission id shows up in the URL
(`/submissions/{submission_id}`) and is how the chain UI is bookmarked
/ shared. Two ways to produce one:

**Author-controlled** — pass a Jinja template to `@form`:

```python
@form(
    title="...",
    form_id="publish_article",
    submission_id="{{ steps.draft.headline | slugify }}",
)
def publish_article_workflow():
    ...
```

The id is **minted when its source value becomes available** — the
runtime checks after every submit (and backend-step run) whether every
`steps.*` the template references has run, and mints the moment they
all have. That node need not be the landing node. `| slugify` (provided
by the framework) lowercases, strips accents, replaces punctuation with
`-`; for "1428 Bayview Ave" the URL becomes `/submissions/1428-bayview-ave`.

Before the id is minted, the submission is a **session draft**: it has
no `submission_id`, is addressed internally by a `handle`, and is not
resumable — leave and it's gone. Once minted it becomes the canonical,
resumable submission.

Policy:
- A fully-resolved template producing an empty result → error.
- Result with characters outside `[A-Za-z0-9_-]` → error. Use
  `| slugify` so URLs are well-formed.
- Collision with an existing minted id → 409 Conflict. Authors who
  want auto-disambiguation can compose:
  `"{{ ... | slugify }}-{{ now() | timestamp_ms }}"`.

**Default** — when `submission_id` is unset, the id is minted at the
first submit (a 10-char uuid, `[a-z0-9]`).

Filters / globals exposed on the Jinja env:

- `| slugify` — ASCII-only, lowercase, alphanumeric+`-`
- `| timestamp_ms` — datetime → ms-since-epoch
- `now()` — current UTC datetime

### Landing page rendering

The frontend's `LandingPage` derives everything from the form
definition via `GET /forms/{form_id}`:

| Source | Renders as |
| --- | --- |
| `@form(title=...)` | Page headline |
| `@form(description=...)` | Page subtitle |
| entry node's `inputs.*` | Form fields |
| entry node's `Button(label)` | Submit button label |

The entry node is the first page's first section node (or the first
top-level node, if no page comes first). Nothing about the LandingPage
is form-specific — swap a different `@form`-decorated workflow in and
the page reflects it.

### External-task hierarchy

Operators that represent external work the form chain waits on share
a base class:

```
ExternalTask                    declares: an id + state alphabet
  └── AirflowStatus              polls an Airflow task instance
      (future) WebhookWait       waits for an HTTP callback
      (future) Timer             waits for a clock
      (future) ManualCallback    waits for an explicit signal
```

The compiler stores them uniformly as `CompiledExternalTask` with a
`kind` discriminator and a per-kind `config` dict. The runtime
dispatches on `kind` when it needs to actually poll the external
system. In 9a the dispatch is uniform mocked timing; later subclasses
plug in real polling without disturbing the chain UI or the DSL
surface.

### DSL primitives

| Primitive             | Role                                                   |
| --------------------- | ------------------------------------------------------ |
| `@form(...)`          | Declares a workflow. Function name is the workflow id. |
| `@page`               | Declares a page — its own navigated view (its own URL) of section nodes, or a flat page. `id=` overrides the id / URL segment. |
| `@node`               | Declares a screen — a page section node or a top-level step. The body returns its layout tree. `id=` overrides the id. |
| `inputs.Text/Integer/Select/TextBlock` | Form inputs. Variable name is the id. |
| `widgets.DistributionFilter`  | Distribution-filter widget (filterable histogram). |
| `displays.Column/Row/Card/Section/Callout` | Layout containers. |
| `displays.Markdown/Divider/Image` | Display leaves. Markdown is the prose workhorse. |
| `@displays.table`     | Read-only key/value table from a function.             |
| `displays.When` / `@displays.branch` | Conditional layout — show fields based on the form's current values. |
| `steps.<node>.<field>` | Reference another node's value — a `{{ template }}`, a field's `options` / `default`, a cross-node `When` condition, or a workflow-backend argument. `steps.<node>` (no field) is a whole-node reference. |
| `inputs.Text/Integer/TextBlock` | Text, numeric, and multi-line inputs.        |
| `inputs.Select/Radio/MultiSelect` | Option-backed inputs (single, single, multiple). |
| `inputs.Checkbox/Date/DateRange/NumberRange/CheckboxGrid/CheckboxList` | Boolean, date, and composite range/matrix/list inputs. |
| `Button(label)`       | Submit button; `>>` wires what runs after it. `variant` styles it (primary/secondary/danger); `url=` makes it a link button instead. |
| `@backend`            | Logic function. Inside a node → submit action wired to a button with operator args; at workflow scope → standalone auto-run step taking explicit `steps` arguments. `hidden=True` hides its chain marker. |
| `@backend.branch`     | Same, plus return value selects the next step.         |
| `AirflowStatus(...)`  | Real Airflow task instance to poll/display.            |
| `>>`                  | Execution edge. Between page/node refs → workflow edges; inside a page body → page-internal section edges. |
| `END`                 | Sentinel returned by `@backend.branch` to terminate.   |

### Branching semantics

`@backend.branch` return values, given a step wired `route() >> [a(), b()]`:
- **A wired downstream id** (`"a"` / `"b"`) → jump to that step. Trailing `AirflowStatus` operators are SKIPPED (mirrors Airflow's BranchPythonOperator).
- **An un-wired id** → the submission fails with a routing error. Targets must be wired with `>>`.
- **`END`** → terminate the workflow. Trailing `AirflowStatus` operators are SKIPPED.
- **`None`** → fall through to the single downstream step (terminal if there is none). Trailing `AirflowStatus` operators DO run. Ambiguous if the branch has multiple downstream steps.

### Templating

Jinja2 syntax: `{{ steps.<node>.<input_or_backend> }}` resolves to a submitted form value or a node-attached `@backend`'s return value; `{{ steps.<backend_step> }}` resolves to a standalone backend step's return value. Used most often in `AirflowStatus.run_id` to thread the trigger's dag-run id through the chain. The same `steps` namespace is available to `@backend` functions as a Python accessor (see "The `steps` accessor" above).

### What's mocked (9a)

For now, `AirflowStatus` operators and the trigger `@backend` are mocked with hand-rolled timing (queued briefly, then ~3s per task). `@displays.table` and `@widgets.histogram` functions are invoked once at compile time. Wiring to real Airflow happens in 9g.

## HTTP API

The frontend speaks to the backend through these endpoints. Every
submission-scoped path is nested under `/forms/{form_id}/` so the
form's ownership is explicit in the URL.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET    | `/forms` | Forms index — every form with `folder_path`, `is_live`, `version_count`, submission counts by state (`running`/`success`/`failed`/`total`), and `last_activity`. Ordered by folder then id. |
| GET    | `/forms/{form_id}` | Pre-submission form schema — `{form_id, title, description, landing_step: {step_id, kind, subject, body, fields, options, xcom, submit_button_label}}`. Drives the landing page. |
| GET    | `/forms/{form_id}/submissions` | A form's submissions, newest first — `submission_id`, `state`, `form_version`, `created_at`, `terminated_at`, `current_step`. 404 if the form is unknown. |
| POST   | `/forms/{form_id}/submissions` | Start a submission. Body: `{values: {...}}` (landing-step form values). Returns the new `Submission`. |
| GET    | `/forms/{form_id}/submissions/{submission_id}` | Full submission state — `{submission_id, form_id, state, started_at, tasks[]}` (the runtime DAG view). |
| GET    | `/forms/{form_id}/submissions/{submission_id}/detail` | The persisted record — every step with the data it captured, plus the event history. Drives the submission summary page. |
| GET    | `/forms/{form_id}/submissions/{submission_id}/steps/{step_id}` | Step schema + xcom + (if submitted) the response payload. |
| POST   | `/forms/{form_id}/submissions/{submission_id}/steps/{step_id}` | Submit a step. Body: `{values: {...}}` — for approval-kind steps include `choice` (the button label) and optional `comments`. |
| POST   | `/forms/{form_id}/submissions/{submission_id}/clear` | Rewind. Body: `{from_task_id?, dry_run?, mode?, scope?}` — `mode` is `reset` (re-open empty, truncate downstream) or `edit` (re-open in place, run the dependency-aware cascade on re-submit); `scope` is `cascade` or `node_only`. Returns `{affected_tasks, cleared}`. |

A submission accessed under a `form_id` it doesn't belong to returns
404 (not 403) — from the client's perspective, the submission doesn't
exist under that form's namespace.

Frontend routes split into two zones. The **console** is the
builder/management surface — overviews and submission tracking. The
**live form** (everything under `/form`) is the end-user-facing form
app, and what the embeddable iframe will eventually serve.

Console:

- `/` — redirects to `/forms`. Becomes a real home screen (high-level
  reporting) later; for now it's just a redirect.
- `/forms` — forms index: every form grouped by folder, with submission
  counts and tracking metadata.
- `/forms/{form_id}` — form summary: the form's identity and
  description, submission/version stats, primary actions, and recent
  submissions.
- `/forms/{form_id}/submissions` — that form's submission list, newest
  first.
- `/forms/{form_id}/submissions/{submission_id}` — submission summary:
  the persisted record — every step with the data it captured, and the
  event history.

Live form:

- `/forms/{form_id}/form` — the fillable form; submitting it starts a
  submission.
- `/forms/{form_id}/form/draft` (+ `/{viewId}`) — draft submission run,
  before the id is minted.
- `/forms/{form_id}/form/submission/{submission_id}` (+ `/{viewId}`) —
  the live submission run.

A path segment is plural only where a real collection page sits behind
it — `/forms` and `/forms/{id}/submissions` have list pages, so they're
plural; under `/form` there is no submission list, so `submission` is
singular.

## Data backend & form versioning

Submissions are persisted to SQLite (via SQLAlchemy) so they survive a
restart. The runtime keeps submissions in memory as its working set;
the database is a durable mirror. The DB file is `backend/data/forms.db`
by default, overridable with the `DB_PATH` env var.

The schema is five tables:

| Table | Holds |
| ----- | ----- |
| `form` | A form's stable identity (the DSL `form_id`), its folder, and whether its file is still present (`is_live`). |
| `form_version` | A snapshot of one compiled state of a form — the render-ready `compiled_graph` JSON plus the DSL `source` it was compiled from. A new row is written only when the compiled structure changes (by content hash). |
| `submission` | One user's traversal, pinned to the `form_version` it ran against. |
| `step` | The submission's *current* execution chain — a reset truncates it. |
| `event` | An append-only lifecycle log (`submission_created`, `id_minted`, `step_started`, `step_submitted`, `step_reset`, `submission_terminated`, `submission_failed`). |

`step` is current-state; `event` is history. A reset truncates the
`step` rows, but the `step_submitted` events retain what was entered —
so nothing is lost.

**Write-through.** After every runtime operation the API mirrors the
submission's state to the database. Persistence begins the moment a
submission's id is *minted* — before that it's an in-memory session
draft and is never written. At boot, every persisted submission is
rehydrated back into the runtime's working set.

**Versioning.** Each scan records the current compiled state of every
form as a `form_version`. A submission is pinned to the version it
started on: new submissions use the latest version, in-flight ones
finish on theirs. Because each `form_version` stores both the rendered
`compiled_graph` *and* the DSL `source`, an old version can always be
both viewed (from the graph) and re-executed (the source is recompiled
on demand). A step removed in a later version therefore still renders
and runs correctly for any submission that began before the change.

## Step 8 — Clear/reset + cross-user staleness

Users can clear/reset tasks, individually or the whole run. Cross-user
staleness — the case where User A clears a run while User B has the
same run open — is mitigated through three mechanisms.

### Clear / reset / edit

Backend: `POST /forms/{form_id}/submissions/{submission_id}/clear` with
optional `from_task_id` (omit for a full clear), `dry_run` (boolean),
`mode` — `"reset"` (re-open the target step empty, truncating
everything after it) or `"edit"` (re-open it in place with its
previously-submitted answers as a draft, leaving downstream steps
materialized) — and, for an edit, `scope`.

A `reset` always truncates downstream. An `edit` runs the **edit
cascade** when the re-opened step is re-submitted: the runtime diffs
the step's old answers against the new ones and walks the steps after
it over their declared dependencies, assigning each a status —
`unaffected` (kept as-is), `needs_review` (a display dependency moved;
the data is still valid but worth a glance), or `needs_input` (a
functional dependency broke — the step is re-opened as a pre-filled
draft to re-confirm). A step nothing depends on is never disturbed.
The dependency graph is the `steps.<node>.<field>` references the
workflow makes: a field's `options` / `default`, a `When` condition, a
`{{ template }}`, and a workflow-level `@backend`'s explicit
arguments. The cascade is precise — an `options` set that shifted but
still contains the submitted choice is `needs_review`, not
`needs_input`; a `When` condition is only `needs_input` if its outcome
actually flips.

The `scope` chooses how far an edit reaches: `"cascade"` (the default)
runs that dependency analysis; `"node_only"` re-submits the edited
step alone and leaves every downstream step exactly as it was — the
author/user takes responsibility for any staleness, the way Airflow's
"clear — only this task" works.

Frontend: `<ResetButton>` is a self-contained component with three
visual variants, opening a confirmation dialog. With `allowEdit`, the
dialog offers three choices — **Reset**, **Edit** (cascade), and
**Edit this step only** (node-only). The DAG badges each step the
cascade touched: a `needs_review` / `needs_input` tag on the node
card. While a step is being actively edited (re-opened, not yet
re-submitted) its card shows a **Cancel edit** action — it reverts the
edit by re-submitting the held answers unchanged, a clean inverse
available only before the edit is committed. Contexts:

- **Per-task** (`variant="icon"`) — small icon button next to each
  task in a `<ProgressNode>` that's in a terminal state.
- **Per-HITL** (`variant="link"`, `allowEdit`) — text link in the
  top-right of a submitted HITL node header; offers reset or edit
  (omitted on the landing step and on a buttonless completion
  screen, which has nothing after it to rewind to).

### Cross-user staleness

1. **Slow background poll on terminal.** Once a run reaches success
   or failed, we stop hammering at 2s but continue at 30s so a remote
   clear surfaces without manual refresh.
2. **`refetchOnWindowFocus: true`.** Returning to the tab refetches
   the run status immediately.
3. **HITL regression invalidation.** When a step's task regresses
   from `success` back to a non-success state (which happens after a
   clear), `useSubmission` invalidates that step's cached
   `stepDetail` so the form re-renders correctly with fresh schema.

## Build plan

- ✅ **1.** Scaffold + landing form
- ✅ **2.** Stateful fake + polling + URL-keyed runs
- ✅ **3.** First visual node
- ✅ **4.** Vertical chain + animated connectors
- ✅ **5.** HITL detection (with auto-scroll, theme system)
- ✅ **6.** HITL submission
- ✅ **7.** Multi-pause + ApprovalOperator + completion node
- ✅ **7.5.** Pluggable widget registry + distribution filter
- ✅ **7.6.** Visualization stack: Visx + chart conventions
- ✅ **7.7.** Widget bundle pattern + chart primitives
- ✅ **8.** Cross-user staleness + clear/reset endpoints + UI
- ✅ **9a.** Python DSL for authoring forms (current) — `@form` /
  `@node`, `@page` callable templates, `>>` orchestration, `@backend` /
  `@backend.branch`, `ExternalTask` hierarchy with `AirflowStatus` as
  the first subclass; submission terminology end-to-end; `slugify` /
  template-driven submission ids
- **9b.** Dynamic landing page from `@form` (current) —
  `GET /forms/{form_id}` returns title, description, and the landing
  step's schema; frontend renders it generically. `FormSummaryNode`
  retired — landing step is a regular HitlNode in the chain.
- **9c.** Admin scaffold
  - `GET /admin/forms` — list of registered forms
  - `GET /admin/forms/{id}` — form definition view (chain preview)
  - `GET /admin/forms/{id}/submissions` — list of in-flight + completed
    submissions
  - `GET /admin/submissions/{id}` — per-step data inspector
- **9d.** Submission persistence (Postgres) — sessions, visited nodes,
  form_values, backend_returns; replaces the in-memory `_submissions` dict
- **9e.** Embeddable form view — iframe-safe CSP + cross-origin
  postMessage for height resize; tokenized links + optional
  public/private modes
- **9f.** Form-author polish — the standard input batch (Radio,
  MultiSelect, Checkbox, Date, DateRange, NumberRange, CheckboxGrid)
  ✓ shipped; conditional layout (`displays.When`, `@displays.branch`,
  `CheckboxList`, label & URL templating) ✓ shipped; the `steps`
  accessor — `options` / `default` and cross-node `When` conditions
  resolved at runtime from an earlier node's input or `@backend` —
  ✓ shipped; button polish — `variant` styling (primary/secondary/
  danger) and `url=` link buttons — ✓ shipped. Next: per-form theme
  customization, then a structural workflow-graph view.
- **9g.** `@page` decorator — visual grouping of nodes. A form can be
  authored as either:
  - **separate pages** — each `@page` renders on its own URL, advancing
    page-by-page; or
  - **sequential** — pages render one after another within a single URL
    (today's chain behavior, but now author-grouped).
  Pages group nodes; nodes are still the atomic step unit. Default when
  no `@page` is declared: every node is its own page (separate-pages
  mode) or all nodes share one implicit page (sequential mode), per
  form-level config.
- **9h.** Auth — admin vs. end-user separation; per-form access control
- **9i.** (When needed) Real Airflow `@backend` pattern —
  apache-airflow-client wired into a reusable helper; one Airflow
  installation can power many forms
- **9j.** Parallel node rendering — layout A on desktop (true
  side-by-side, Airflow graph-view style), layout B on mobile
  (stacked-with-bracket fallback). Backend exposes a level/lane index
  per task derived from the dep graph
- **9k.** More `ExternalTask` subclasses as needed — `WebhookWait`,
  `Timer`, `ManualCallback`
- **10.** Redis read-through cache (lower priority)
- **11.** SSE for active submissions

## Running

### Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173.

## Project structure

```
project/
├── backend/
│   ├── main.py                FastAPI app — endpoints driven by the DSL runtime
│   ├── requirements.txt
│   ├── workflows/             ◆ DSL framework
│   │   ├── core.py            @form / WorkflowTemplate, @page / @node /
│   │   │                      NodeTemplate, Operator base, graph
│   │   ├── inputs.py          Text / Integer / Select / Radio / MultiSelect /
│   │   │                      Checkbox / Date / DateRange / NumberRange /
│   │   │                      CheckboxGrid / CheckboxList / TextBlock
│   │   ├── actions.py         Button
│   │   ├── displays.py        Column/Row/Card/Section/Callout, Markdown/Divider/Image, @table
│   │   ├── conditions.py      When container, FieldCondition, @displays.branch
│   │   ├── references.py      steps accessor — cross-node options/default
│   │   ├── widgets.py         DistributionFilter (filterable histogram)
│   │   ├── backend.py         @backend, @backend.branch
│   │   ├── external.py        ExternalTask base + Airflow operators
│   │   ├── airflow_hook.py    REST client over Airflow's /api/v2 API
│   │   ├── airflow_dispatch.py  Per-operator polling logic
│   │   ├── crypto.py          Fernet encryption for stored credentials
│   │   ├── templating.py      Jinja2 — `{{ steps.X.Y }}` resolution
│   │   ├── compile.py         Workflow definition → runtime data + serialization
│   │   ├── store.py           SQLAlchemy persistence — forms, versions, submissions, connections
│   │   └── runtime.py         Workflow run state machine, mock timing
│   ├── workflows_user/        ◆ user workflow definitions
│   │   ├── publish_article.py      Airflow-orchestration demo
│   │   ├── speaker_submission.py   Input + conditional-layout showcase
│   │   └── input_gallery.py        Standard input gallery
│   └── data/                  SQLite database (created at runtime)
└── frontend/
    └── src/
        ├── main.tsx           applies theme on boot
        ├── App.tsx
        ├── theme/             ◆ all visual styling lives here
        ├── lib/               api.ts, dagState.ts, chainSegments.ts, format.ts
        ├── hooks/             useFormDetail, useStartSubmission, useSubmission,
        │                      useStepDetail, useSubmitStep, useFormsList,
        │                      useFormSubmissions, useSubmissionDetail
        ├── components/
        │   ├── dag/           StatusIndicator, DagNode, DagChain, ProgressNode,
        │   │                  HitlNode, BackendStepNode, ResetButton
        │   ├── forms/         Field, TextField, NumberField, SelectField,
        │   │                  TextareaField, CheckboxField, RadioField,
        │   │                  DateRangeField, NumberRangeField,
        │   │                  MultiSelectField, CheckboxGridField,
        │   │                  CheckboxListField,
        │   │                  SubmitButton, DynamicForm, ApprovalForm
        │   ├── listing/       StatePill
        │   ├── widgets/       widget registry + DistributionFilterWidget
        │   ├── charts/        Visx-based chart primitives + DescriptiveStats
        │   └── ui/            Modal
        └── pages/             LandingPage, SubmissionPage, FormsListPage,
                               FormSummaryPage, FormSubmissionsPage,
                               SubmissionSummaryPage
```
