# frontflow — packaging notes

frontflow is a pip-installable package. The repo layout:

    pyproject.toml          package definition
    src/frontflow/          the package
      __init__.py           re-exports the authoring DSL
      main.py               FastAPI app (API + serves the bundled UI)
      cli.py                the `frontflow` command
      dsl/                  authoring API + runtime
      static/              the pre-built web UI (shipped in the wheel)
      examples/             bundled demo workflows + Airflow DAGs
    frontend/               web UI source (only needed to rebuild static/)

## Build the wheel

    python -m build --wheel

The web UI is pre-built into src/frontflow/static/ and ships inside the
wheel — an installing user never runs npm.

## Rebuilding the web UI (only if frontend/ changes)

    cd frontend && VITE_API_URL="/api" npm install && npm run build
    rm -rf ../src/frontflow/static && cp -r dist ../src/frontflow/static

VITE_API_URL="/api" makes the UI call the API at the /api prefix on the
same origin (the API and the UI are served by the same process).

## Install & run

    pip install frontflow-1.0.0-py3-none-any.whl
    frontflow examples ./my-workflows      # copy the demos to start from
    frontflow serve ./my-workflows         # API + web UI on one port

The `serve` source can be a local directory or an S3 location:

    frontflow serve ./my-workflows               # local directory
    frontflow serve s3://my-bucket/forms         # S3 (needs the s3 extra)

For S3, install the extra and provide AWS credentials the usual way
(environment, shared config, or an instance role):

    pip install "frontflow[s3]"

## Admin console authentication

The admin console — connection management, workflow refresh, theme
editing — requires a signed-in account. Bootstrap one:

    frontflow create-admin --username <name>

(prompts for a password if omitted). Then sign in at /login. Until an
account exists, the admin endpoints return 503. Form rendering and
submission stay public — end users never sign in.

Sessions are server-side, carried in an HttpOnly cookie, and survive a
restart. `create-admin` honors `--env-file` so it writes to the
configured database.

## User management

The console's Users page (admin only) creates accounts, resets
passwords, toggles admin, and activates/deactivates or deletes users —
`create-admin` / `create-user` remain as the CLI bootstrap and
recovery path. A reset password is temporary: the user must set a new
one at next sign-in. Any signed-in user can change their own password.
Safeguards: the last admin cannot be demoted, deactivated, or deleted,
and an admin cannot remove their own access; password resets,
demotions, and deactivations end the affected user's existing sessions
immediately.

## Folder permissions

Non-admin operators see only the forms they are granted. Create one
with `frontflow create-user --username <name>`, then in the console's
Access page: make a group, add the user, and grant the group a role
on a folder. A grant cascades — `billing` covers `billing` and every
form beneath it. Roles: `view` (see and track) and `manage` (also edit
theme, reparse). Admins see and manage every form. Folders are just
form paths — a grant may name a folder before any form lives there.

## Form visibility

Each form has a visibility mode, set on its summary page (needs manage
access). `public` — anyone with the link can fill it. `unlisted` — only
people with the form's private link (it carries an unguessable token;
regenerating the token invalidates old links). `restricted` — only the
signed-in users on the form's allow-list. A folder grant or admin
always confers access regardless of the mode. An unauthorized visitor
gets a not-found response — a restricted or unlisted form's existence
is not revealed. New and existing forms default to `public`.

## File uploads

Two upload inputs. `inputs.File` is transient — the uploaded bytes
reach the backend and are handed to `@backend` functions as a handle
(`.read()`, `.bytes`, `.filename`, `.content_type`, `.size`), then
discarded; nothing is persisted. `inputs.S3File` persists the bytes to
S3 — the submission value carries `bucket`, `key`, `filename`, `size`,
`content_type`, and a `@backend` function gets a handle whose
`.read()` fetches the bytes back and `.url()` returns a presigned
link. Both take `accept` (allowed extensions) and `max_size_mb`
(default 25), enforced server-side.

S3File needs the `s3` extra (`pip install frontflow[s3]`). The AWS
connection holds **credentials only** — access key id, secret access
key, optional session token, optional region. The bucket is set on
each `S3File`, not on the connection: `bucket=` is required, since
credentials and storage targets are separate concerns. Credentials
resolve from a stored `aws` connection first, then boto3's default
chain. A misconfigured S3File fails the upload loudly rather than
dropping the file.

S3File takes a required `key` — the S3 object key, which is
templatable. It may contain `{{ steps.<node>.<field> }}` tokens (with
filters such as `| slugify`) and a literal `{filename}` placeholder
that expands to the uploaded file's name, e.g.
`key="receipts/{{ steps.intake.client | slugify }}/{filename}"`. The
key resolves to an exact path — there is no anti-collision segment, so
a key matching an existing object overwrites it; template in a unique
value (a submission id, a name) to avoid that. Tokens naming an
earlier step resolve against the submission's data; tokens naming the
upload's own screen resolve against that screen's values at the moment
of upload (a snapshot — later edits do not move the file).

## Sankey mapping input

`inputs.Sankey` is a weighted many-to-many mapping between two columns
of values, drawn as a Sankey diagram. The user clicks a column-A value
then a column-B value to connect them; each connection carries a
weight, shown as a ribbon whose thickness tracks it. `column_a` /
`column_b` may each be a static list or an `steps.<node>.<field>`
reference resolved at runtime (e.g. an earlier MultiSelect's choices).
With `normalize=True` (the default) each source's weights must total
100 — the percentage case — and the editor flags sources that are off.
The submitted value is a list of `{from, to, weight}` triples.

## Submission export API

A pull-based REST API for batch-collecting submission data into an
external system. Set a bearer token to enable it:

    FRONTFLOW_API_TOKEN=<a-long-random-string>

    GET /api/export/submissions
      Authorization: Bearer <token>

Returns a page of submission records (all states, in-flight included)
and a `next_cursor`. Walk the whole set by passing `next_cursor` back
until it is null.

`updated_since` and `updated_before` (ISO 8601) bound the last-update
time as a half-open interval [since, before). Re-running a fixed
window always returns the same rows, so a windowed pull is safely
retryable. Either bound may be given alone; `updated_since` by itself
is open-ended.

The feed is at-least-once. A submission's update time moves on every
state change, so an in-flight submission can appear in several
successive windows, each time in a newer state — consumers upsert on
`submission_id`. Pass `terminal_only=true` to restrict to terminated
submissions, whose update time is frozen: each then falls in a single
window (unless an operator later retries it, which is genuinely new
data). Use `terminal_only` for a warehouse load that wants each
submission once; omit it to also see live in-flight submissions.

Optional `form_id` restricts to one form; `limit` sets page size
(max 500). If FRONTFLOW_API_TOKEN is unset the endpoint returns 503 —
it never serves submission data open.

## Database

By default frontflow uses a SQLite file in a per-user data directory
(~/.frontflow/forms.db) — zero setup. For a shared or production
deployment, point it at Postgres with DATABASE_URL:

    pip install "frontflow[postgres]"
    DATABASE_URL=postgresql+psycopg://user:pass@host:5432/frontflow

DB_PATH still names a specific SQLite file; FRONTFLOW_HOME relocates the
default data directory.

Configuration can be supplied in a .env file instead of exported
variables:

    frontflow serve --env-file ./frontflow.env

The file is loaded before startup; it can set WORKFLOW_SOURCE,
DATABASE_URL, FRONTFLOW_SECRET_KEY, AWS credentials, etc. Variables
already set in the real environment take precedence over the file.

Push or edit workflow files in the source, then POST /api/refresh (all
forms) or POST /api/forms/{id}/refresh (one form) to pick them up — no
restart needed. Workflow files are executable Python; point frontflow
only at a location you control.

## Authoring

A workflow file imports from the package root:

    from frontflow import form, node, inputs, displays, Button
