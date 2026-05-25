# contact_form — frontflow vs hand-rolled

A side-by-side artifact for a **simple contact form**: name, message,
a radio choice for preferred contact method, and a conditional
follow-up field that asks for a different detail based on the
radio choice (email / phone / mailing address). Submit advances to
a personalized thank-you page.

Both versions are in this directory:

- [`frontflow_version/contact_form.py`](frontflow_version/contact_form.py) —
  the form written with the frontflow DSL
- [`bare_version/`](bare_version/) — a hand-rolled FastAPI app
  doing the same thing

## Line counts

| | Total lines | Non-blank / non-comment | Files |
|---|---:|---:|---:|
| **frontflow DSL** | **101** | **84** | **1** |
| **Bare FastAPI** | **350** | **297** | **6** |
| Ratio | ~3.5× | ~3.5× | 6× |

The ratio is higher than the
[publish_article comparison](../publish-article-without-frontflow/README.md)
(~2×) because contact forms don't have a heavy backend stack to
share — there's no Airflow DAG file pulling the bare-version line
count up. The full cost of the user-facing form falls on the bare
version.

## What the bare version contains

| File | Lines | What it does |
|---|---:|---|
| `app/main.py` | 195 | FastAPI routes, DB model, validation, conditional-field server logic |
| `templates/base.html` | 12 | Shared HTML shell |
| `templates/form.html` | 95 | The form: three always-present fields + three conditional follow-up blocks + a small JS snippet to reveal the matching one |
| `templates/thanks.html` | 6 | Personalized confirmation page |
| `static/style.css` | 42 | Minimal hand-rolled CSS |
| `requirements.txt` | 5 | FastAPI, uvicorn, Jinja2, SQLAlchemy, python-multipart |

The frontflow version is one file. The bare version is six.

## What both versions provide

User-facing parity:

- Three always-required fields: name, message, contact method (radio)
- A conditional follow-up that reveals one of three blocks based on
  the radio choice — email with format validation, phone with format
  validation and an "OK to text" checkbox, or a mailing-address
  textarea
- Server-side validation with inline error messages on the
  always-required fields *and* the conditional follow-up
- Submission persisted to SQLite
- A personalized confirmation page interpolating the submitted name
  and contact method

## What the bare version does NOT provide (that frontflow does)

| frontflow gives you | Bare version |
|---|---|
| Admin UI listing every submission, drill-down, search, filters | not built |
| Analytics dashboard with throughput, completion rate, branch distribution | not built |
| Form versioning — schema evolution doesn't break old submissions | not built |
| Submission export endpoints | not built |
| Auth + per-form access control | not built |
| Themed inputs with design tokens, dark mode, full design system | not built (42 lines of hand CSS) |
| Field-level validation that's declarative, not regex-in-Python | bare: hand-rolled regex with custom error messages per field |
| Conditional reveal as a declarative `@displays.branch` | bare: HTML for all three blocks + inline JS to toggle |
| Submission state machine (running / success / failed) with timestamps | bare: a single row write, no lifecycle |
| Cross-step templated values (`{{ steps.intake.full_name }}`) in displays | bare: explicit variable passing into the template context |
| Edit a past submission and recompute downstream work | not supported — no edit flow at all |

## Where the difference shows up

A side-by-side of the conditional reveal — the most distinctive part
of this form:

### frontflow (5 lines of declaration)

```python
@displays.branch
def contact_details(contact_method):
    if contact_method == "Email":
        return (inputs.Email(label="Email address", required=True),)
    elif contact_method == "Phone":
        return (inputs.Phone(label="Phone number", required=True),
                inputs.Checkbox(label="OK to send texts"))
    else:
        return (inputs.TextBlock(label="Mailing address", required=True),)
```

That's it. frontflow handles client-side reveal, server-side
validation gating ("only require email when method=Email"), and the
form re-render with errors.

### bare version

Three concerns split across three files:

**HTML** — three `<div class="conditional" data-show-when-method="...">`
blocks, each with its own inputs and `hidden` attribute managed by
server-side state.

**JS** — a small inline script that watches the radio buttons and
toggles the `hidden` attribute on the conditional blocks.

**Server validation** — three branches of `if/elif/else` in the
submit handler that conditionally require the right field based on
the radio value.

The pieces individually are small. Knowing how to write all three
correctly, keeping them in sync as the form changes, and re-rendering
errors with the right block visible — that's the real cost.

## How to run

### frontflow version

From the package root:

```bash
mkdir /tmp/contact_demo
cp docs/comparisons/contact-form/frontflow_version/contact_form.py /tmp/contact_demo/
frontflow serve /tmp/contact_demo
```

Open <http://127.0.0.1:8000/forms/contact_form>.

### bare version

```bash
cd docs/comparisons/contact-form/bare_version

python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000/>.

## Honest caveats

- **The bare version's conditional reveal uses inline JS.** A
  no-JS-required version would either submit on every radio change
  (one round-trip per choice) or render all three follow-up fields
  always-visible. Both are worse UX than the frontflow version. The
  inline JS captures the realistic minimum.
- **The frontflow version has 84 non-blank/non-comment lines.** A
  good chunk of that is whitespace and docstring; the *actual form
  declaration* is closer to 50 lines. The DSL is denser than the
  count suggests.
- **The bare-version regex validation for email and phone is
  intentionally lightweight.** Real production validation would use
  a library (`email-validator`, `phonenumbers`) and that adds more
  imports and more code paths. frontflow's `inputs.Email` and
  `inputs.Phone` handle this — the bare version's simplistic
  validation is generous to the bare side.
- **I have not run the bare version end-to-end.** It's structurally
  complete and the imports are correct, but the conditional-reveal
  JS, the server-side validation branches, and the personalized
  thank-you page are written from familiarity with the stack, not
  from a live test. The point is to compare code volume, not to
  ship a production form.
- **The frontflow version *has* been verified to compile and
  register** under a fresh frontflow install. The form id, title,
  and node chain (`intake → thanks`) were confirmed against the
  package before this writeup shipped.
