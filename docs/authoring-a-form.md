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
