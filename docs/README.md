# frontflow docs

Repo-level documentation. These files live in version control alongside
the source but are **not** packaged into the wheel — they're for people
reading the source repo, not for runtime consumption.

For the user-facing package README, see [`../README.md`](../README.md).
For the development roadmap, see [`../ROADMAP.md`](../ROADMAP.md).

## Start here

- [**What is frontflow?**](what-is-frontflow.md) — what the package
  is, what it's for, what it's not.
- [**Quickstart**](quickstart.md) — install and run a bundled example
  in under five minutes.
- [**Authoring a form**](authoring-a-form.md) — write your own
  hello-world from scratch and learn the conventions as you go.
- [**Deploying**](deploying.md) — taking frontflow to production:
  configuration reference, Postgres setup, encryption key, and
  platform recipes (Heroku, Docker, generic VPS with systemd).
- [**Embedding forms**](embedding.md) — embed a form in an iframe on
  another origin: allowlist syntax, security model, the bundled
  `embeddable_signup` example, and the longer arc (per-node,
  authenticated).

## Reference

### Comparisons

Side-by-side artifacts showing what user-facing workflows look like
with frontflow versus without it. Built to answer "what does the DSL
actually save me?"

- [`comparisons/contact-form/`](comparisons/contact-form/) — a
  simple two-node contact form with a conditional follow-up on
  preferred contact method. Smallest comparison in the directory;
  good first read.
- [`comparisons/publish-article-without-frontflow/`](comparisons/publish-article-without-frontflow/) —
  the bundled `publish_article` example rewritten as a bare FastAPI
  app. Adds Airflow, HITL, and branching to the comparison surface.

## Design

Internal design docs. **Drafts for framework-author review** — not
user-facing, not stable. They settle the model for a feature
before any code lands.

- [`design/role-based-assignment.md`](design/role-based-assignment.md) —
  the design for role-based access, form-to-form assignment,
  external user identity (Canvas-SIS-id model), in-app inbox,
  signed-link delivery, and filtered iframe embedding. Status:
  DRAFT v2 for review. ~1170 lines.
