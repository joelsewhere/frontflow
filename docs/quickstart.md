# Quickstart

Get a frontflow form running locally in five minutes.

## Install

```bash
pip install frontflow
```

(Wheel works too: `pip install frontflow-1.0.0-py3-none-any.whl`.)

## Run a bundled example

frontflow ships with a handful of example forms. The simplest is
`quickstart` — one page, three inputs, no backend.

Install it into a forms directory:

```bash
frontflow example install quickstart --dest ./forms
```

Then serve:

```bash
frontflow serve ./forms
```

That prints something like:

```
frontflow: serving workflows from /Users/you/forms
frontflow: http://127.0.0.1:8000
```

Open the form in a browser:

> **<http://127.0.0.1:8000/forms/quickstart>**

The form is public — no login needed. Submit it; the response is
captured to the database.

## What else is at the server

| URL | Visibility | What it shows |
|---|---|---|
| `http://127.0.0.1:8000/forms/quickstart` | Public | The form, ready to submit |
| `http://127.0.0.1:8000/forms` | Login required | The admin listing of every form |
| `http://127.0.0.1:8000/submissions/{handle}` | Login required | A submission's current state |
| `http://127.0.0.1:8000/forms/quickstart/analytics` | Login required | Throughput, completion rate, and the rest of the analytics view |

For the admin pages, create an admin user (in another terminal,
while the server is still running):

```bash
frontflow create-admin --username admin --password admin
```

Then log in at <http://127.0.0.1:8000/login> with `admin` / `admin`.

## See more examples

```bash
frontflow example list
```

Each example has a one-line description. Install one with the same
`example install` command, or install all of them:

```bash
frontflow example install --all --dest ./forms
```

The `input_gallery` example shows every input type; `publish_article`
shows an end-to-end Airflow pipeline with editor review; and so on.

## Optional: seed the analytics with realistic submissions

A fresh install has no submission data, so the analytics dashboards
are empty. Seed them with simulated submissions:

```bash
frontflow example seed --all --count 100 --days 30 --source ./forms/examples
```

That fakes 100 submissions spread across the last 30 days for every
example form. Open `/forms/<form_id>/analytics` to see the charts
populate.

## Configuration

Everything works with defaults — no env file needed for local
development:

- **Database:** SQLite at `~/.frontflow/forms.db`.
- **Encryption key:** auto-generated at `~/.frontflow/secret.key`
  the first time the server starts.
- **Workflow source:** the directory you pass to `serve`, falling
  back to `WORKFLOW_SOURCE` env, falling back to `.`.

For production deployments, set these explicitly via env vars or a
`.env` file passed with `--env-file`:

| Variable | What it does |
|---|---|
| `DATABASE_URL` | Postgres / MySQL connection string instead of the default SQLite |
| `DB_PATH` | SQLite path if you're keeping SQLite but want it somewhere specific |
| `FRONTFLOW_SECRET_KEY` | Fernet key for encrypting credentials in the connection store. Auto-generated for local dev, but set explicitly in production so it survives restarts and stays consistent across replicas. |
| `WORKFLOW_SOURCE` | Where to look for workflow files (a directory or an `s3://` URI) |

```bash
frontflow serve --env-file .env
```

## Where to go next

- [`authoring-a-form.md`](authoring-a-form.md) — write your own
  hello-world form from scratch
- [`what-is-frontflow.md`](what-is-frontflow.md) — what frontflow
  is for and what it's not
