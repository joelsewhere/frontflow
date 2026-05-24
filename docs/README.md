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

## Reference

### Comparisons

Side-by-side artifacts showing what user-facing workflows look like
with frontflow versus without it. Built to answer "what does the DSL
actually save me?"

- [`comparisons/publish-article-without-frontflow/`](comparisons/publish-article-without-frontflow/) —
  the bundled `publish_article` example rewritten as a bare FastAPI
  app. Includes line counts, feature-parity table, and honest
  caveats about what the bare version omits.
