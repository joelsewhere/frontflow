# What is frontflow?

A **form builder authored as Python code**. You write a `.py` file
declaring a multi-step form using a small decorator-based DSL;
frontflow serves it as a webpage, captures submissions, persists
state, and routes the user through the chain. An admin UI for
inspecting submissions and analytics comes built-in.

## What you write

A workflow file. Looks like this:

```python
from frontflow import Button, form, inputs, node

@form(title="Bug report")
def bug_report():

    @node
    def report():
        summary = inputs.Text(label="One-line summary", required=True)
        steps = inputs.TextBlock(label="Steps to reproduce")
        return summary, steps, Button("Submit")

    report()

bug_report()
```

That's a complete, working form. Put it in a directory, run
`frontflow serve <dir>`, and a browser-renderable form appears at
the URL printed in the terminal.

## What you get

For that file, frontflow gives you:

- **An HTML form** rendered to your design system, with real
  client-side behavior (validation, error messages, required-field
  enforcement)
- **A submissions database** (SQLite by default, Postgres if you
  point it at one) that captures every submission with timestamps
- **An admin UI** at `/forms` that lists every form in the directory
  and lets you drill into any submission to see its values,
  timestamps, and current step
- **An analytics view** per form with throughput, completion rate,
  branch distribution, and time-series charts
- **Submission export endpoints** for pulling data into downstream
  systems
- **Form versioning** — when you edit the workflow file, prior
  submissions still render and resolve against the version they
  were started under
- **Auth + per-form access control** when you want it; public
  forms when you don't

You did not write any of the above. You wrote 10 lines of form
declaration.

## What it's for

frontflow handles forms of any shape — a one-question NPS survey, a
job application, a multi-step regulated data-intake wizard, or a
form that kicks off an Airflow pipeline and waits for a human
reviewer's decision.

What you write is a Python file. What you get is a hosted form
with themed inputs, server-side validation, a submissions database,
an admin UI for inspecting submissions, and analytics dashboards
per form. Backend hooks are optional — declare them when you want
the form to drive real work; skip them when you just want to
collect responses.

## What it's not

- **Not a hosted SaaS.** You run the server. The data is yours.
- **Not a no-code tool.** The DSL is Python. The author writes code.

## Where to go next

- [`quickstart.md`](quickstart.md) — install and run your first
  form in under five minutes
- [`authoring-a-form.md`](authoring-a-form.md) — the conventions
  for writing a form, walked through with hello-world
- [`comparisons/publish-article-without-frontflow/`](comparisons/publish-article-without-frontflow/) —
  what a real form looks like with frontflow versus hand-rolled
  FastAPI; line-count comparison and feature-parity table
