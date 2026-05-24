# publish_article — what it looks like without frontflow

A side-by-side comparison of the bundled `publish_article` example
written **with** the frontflow DSL versus a hand-rolled **bare
FastAPI** equivalent.

The frontflow version lives at:

    src/frontflow/examples/publish_article.py

The bare version lives in this directory.

Both versions drive the same Airflow DAG, which is included in the
package at:

    src/frontflow/examples/airflow_dags/publish_article_dag.py

The DAG file is needed in both versions (it runs in Airflow regardless
of how the user-facing form is built), so it's excluded from the
comparison below.

## Line counts

| | Total lines | Non-blank / non-comment | Files |
|---|---:|---:|---:|
| **frontflow DSL** | **213** | **162** | **1** |
| **Bare FastAPI** | **422** | **359** | **9** |
| Ratio | ~2.0× | ~2.2× | 9× |

The "1 file vs 9 files" is the more honest axis. Line count is easy
to game; file count tracks "how many concepts does the developer have
to hold in their head" more directly.

## What the bare version contains

| File | Lines | What it does |
|---|---:|---|
| `app/main.py` | 288 | FastAPI routes, DB model, Airflow REST client, state machine |
| `templates/base.html` | 13 | Shared HTML shell |
| `templates/draft.html` | 45 | The form itself: five inputs + validation surfaces |
| `templates/waiting.html` | 6 | "Working…" page that auto-refreshes during polling |
| `templates/approved.html` | 9 | Approve branch landing |
| `templates/article_live.html` | 6 | Approve branch terminal with published URL |
| `templates/changes_requested.html` | 7 | Request-changes branch terminal |
| `templates/rejected.html` | 5 | Reject branch terminal |
| `static/style.css` | 43 | Minimal hand-rolled CSS |

## What the bare version provides

User-facing parity with the DSL example:

- Form rendering: Text, TextBlock, Radio, Checkbox, submit button
- Server-side validation with inline errors
- Submission persisted to SQLite, keyed by a slug-of-headline id
- Airflow DAG triggered with the submission as `conf`
- Polls `build_content` task, then `editor_review` HITL task
- Branches to one of three terminal pages on the editor's HITL decision
- Approve branch has a second button that pulls the published URL via XCom

## What the bare version does NOT provide (that frontflow does)

Each of these would be its own multi-file build-out to match:

| frontflow gives you | Bare version |
|---|---|
| Admin UI with every submission, drill-down, search, filters | not built |
| Edit a past answer → downstream Airflow runs auto-clear and rerun | not built (edits not supported at all) |
| Form versioning — schema evolution doesn't break old submissions | not built |
| Auth + per-form access control | not built (every route is public) |
| Resumable URL handles | partial (URL works, no resume-from-draft) |
| HTML/CSS theming via design tokens, dark mode, full design system | not built (43 lines of hand CSS) |
| Submission export endpoints | not built |
| Analytics dashboard with charts, throughput, branch distribution | not built |
| Connection store (Airflow creds via UI, encrypted at rest) | not built (env vars) |
| Declarative branch routing: `draft >> [a, b, c]` | bare: hand-rolled if/elif state machine |
| Cross-step templated values: `{{ steps.draft.headline }}` | bare: function-arg passing, no cross-step templating |
| `@backend` Python hook between submit and DAG trigger | bare: inlined into the submit route |
| Mock Airflow connection for local dev — runs end-to-end with no Airflow | not built (real Airflow required) |
| Submission state machine serialized via `_submissions_lock` | bare: race-condition vulnerable |
| Retry / backoff on Airflow API calls | not built |

## Why only ~2×, not 10×

Three honest reasons:

1. **The DAG is the same.** A lot of the work — content build, HITL
   gating, publish, XCom — happens in Airflow. Frontflow doesn't
   shrink the DAG.

2. **This is the minimum viable user path.** The bare version skips
   the admin UI, edit-cascade, versioning, auth, analytics, and
   theming that frontflow gives you for free. Each of those would
   balloon the line count.

3. **FastAPI + Jinja2 + SQLAlchemy is already productive.** A naive
   Django or Rails equivalent would be longer; a React frontend would
   add another 200–300 lines on top.

If the bare version had to match frontflow's full feature set, the
realistic estimate is **5–10× the lines**, spread across:

- Admin app (submission listing, filters, drill-down): ~500–1000 LOC
- Cascade-clearing logic: ~200–400 LOC
- Form versioning + schema migration: ~300–500 LOC
- Auth and per-form access: ~150–300 LOC
- Analytics dashboard: ~400–800 LOC
- React frontend instead of Jinja templates: +200–300 LOC

## How to run the bare version

```bash
cd docs/comparisons/publish-article-without-frontflow

python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

export AIRFLOW_API_URL=https://airflow.example.com/api/v1
export AIRFLOW_USER=admin
export AIRFLOW_PASS=changeme

# Default SQLite at ./publish.db; override with DATABASE_URL=
uvicorn app.main:app --reload
```

Then open <http://localhost:8000/>.

## How to run the frontflow version

Install the package, copy the example into a forms directory, run:

```bash
frontflow example install publish_article --dest ./forms
frontflow serve ./forms
```

See the package root README for full setup, including connection
store config that lets the form drive a real Airflow.

## Caveats on this comparison

- **I wrote both versions** (in collaboration with the frontflow
  author over many sessions). A fresh engineer might find ways to
  shorten the bare version — though probably not below ~1.5×.
- **The bare version's Airflow REST shape** is written from familiarity
  with the API surface, not from end-to-end testing against a live
  Airflow. Some endpoint paths or response shapes may need
  adjustment for your Airflow version + HITL provider.
- **The HITL polling** assumes the HITL task XComs its chosen option
  under key `"hitl_choice"`. Airflow's HITL operators work this way;
  third-party HITL providers may differ. Frontflow's `HitlBranch`
  abstracts this; the bare version exposes it.
- **Race conditions on the state machine.** Two concurrent
  `/status/{id}` polls could both observe `building`, both flip to
  `reviewing`, both poll the HITL decision, both flip on. Frontflow
  serializes this via `runtime._submissions_lock`. The bare version
  does not.
- **No retry / backoff on Airflow calls.** A flapping network surfaces
  500s directly to the user. Frontflow's connection layer retries.
