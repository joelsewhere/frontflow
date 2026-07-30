# Form Builder — Build Roadmap

Step tracking for the active build. Status markers: ✅ done · 🔄 in progress · ⬜ pending.

## Airflow integration

A connection store plus Airflow hook/operators, so a form's workflow can trigger
DAGs, poll tasks, pull XComs, and run human-in-the-loop steps against a real
Airflow 3.1+ instance (`/api/v2` REST API).

| Step | Scope | Status |
|------|-------|--------|
| 1 | **Connection store** — encrypted credential store (Fernet), `connection` table, REST API, console `/connections` page with a type registry | ✅ |
| 2 | **AirflowHook** — REST client over Airflow's `/api/v2` API; JWT auth; trigger / dag-run / task-instance / xcom / HITL methods | ✅ |
| 3 | **Operators** — `TriggerDag`, `AirflowTaskSensor`, `AirflowDagSensor`, `XComPull` + the `graph_visible` flag; compile support | ✅ |
| — | **Runtime wiring · dispatch** — `airflow_dispatch.py`, the real per-operator polling logic | ✅ |
| — | **Runtime wiring · integration** — `external_state` on steps, `_process_real_chain` in `advance()`, namespace threading, real states to the frontend | ✅ |
| 4 | **Graph integration** — `airflow` node kind: backend graph builder, frontend node renderer + legend | ✅ |
| 5 | **AirflowHitl** — operator, HITL dispatch state machine, response endpoint, form-side response UI | ✅ |
| — | **Edit-aware Airflow clearing** — an edit that clears a frontflow step also clears the Airflow task instances those steps own: a whole DAG run for an affected `trigger_dag`, the linked task instance for an affected sensor/xcom/HITL operator; frontflow-visible redundancy deduped. A cleared `trigger_dag` given an *explicit* run id re-attaches to the cleared run on replay (rather than POSTing a duplicate) when the run id re-resolves unchanged; the re-attach stash is persisted (`submission.cleared_run_ids`) and consumed once used. Fails loud if Airflow rejects a clear. | ✅ |
| 6 | **Demo workflows** — demo 1 `publish_article` ✅; demo 2 `speaker_submission` ✅; demo 3 `expense_reimbursement` ✅ | ✅ |

## Submission export API

A pull-based REST API so an external consumer can batch-collect submission
data (all states, including in-flight). Keyset cursor for a full walk plus
`updated_since` for incremental re-sync. Secured with a static bearer token.

| Step | Scope | Status |
|------|-------|--------|
| 1 | **`Submission.updated_at`** — column + bump on every state change; portable migration backfilling existing rows | ✅ |
| 2 | **Export endpoint** — `GET /api/export/submissions`: keyset `cursor`, half-open `[updated_since, updated_before)` interval, `terminal_only`, `form_id` filter, `limit`; axis-consistent cursor; at-least-once feed (upsert on `submission_id`); per-submission records with a nested compact `steps` array | ✅ |
| 3 | **Bearer-token auth on the export endpoint** — `FRONTFLOW_API_TOKEN`; refuses to serve if unset (no accidental open exposure) | ✅ |

## Service-wide authentication — PRIORITY MILESTONE

**Hard requirement.** frontflow will be exposed to a production Airflow service
— it holds submitted form data and can trigger real DAGs, so an open API is not
acceptable for that deployment. This is the next milestone after the export API.

Needs a real design pass before building. Open questions to settle first:

- Identity model: org-wide gate (one shared credential) vs. user accounts with login.
- Per-form authorization: does user X see form A but not form B, or is access all-or-nothing?
- Web UI auth: session cookies vs. SSO/OIDC; login screen.
- API auth: bearer tokens / API keys for machine callers (the export token is a first instance of this).
- Surface: every `/api` route gated; the connection store (Airflow credentials) is especially sensitive.

Settled by design discussion: composition is **additive** (folder access OR
form visibility grants entry); identity is **hosted accounts now, SSO later**.
Sequenced as three milestones.

| Step | Scope | Status |
|------|-------|--------|
| 0 | **Design** — additive composition; hosted-then-SSO identity; 3-milestone sequencing | ✅ |
| M1 | **Auth foundation (hosted)** — `User`/`AuthSession` tables, argon2id passwords, DB-backed sessions, `frontflow create-admin`, login/logout/me endpoints, `require_admin` gate on the console surface, login screen; admin surface 503s until an account exists | ✅ |
| M2 | **Folder-level group permissions** — groups, user↔group membership, hierarchical (prefix) folder grants, view/manage roles, per-user form filtering, `create-user` CLI, console Access page | ✅ |
| M3 | **Per-form visibility** — public / unlisted / restricted modes, per-form ACL, unlisted-link tokens, visibility check on the form-filling surface; additive against folder grants; unauthorized → 404 | ✅ |
| ✓ | **User management UI** — console account creation, admin password reset (forced change at next login), self-service change-password, admin toggle, activate/deactivate, delete; last-admin and self-action guards; sessions revoked on security changes | ✅ |
| — | **Email-based password reset** — self-service "forgot password" via an email link. Deferred — no email infrastructure in the system yet; admin-mediated reset (with forced change) covers the need for now. | ⬜ |
| — | **Form preview / read-only mode** — let a permitted user open a form's fields and layout without submitting (stakeholder review, "show what you'd fill"). A render-mode on the form-filling surface, distinct from M3's reach-modes. | ⬜ |
| — | **SSO / OIDC** — pluggable external identity; group claims; layered onto the same authz tables | ⬜ |

## V1 input expansion

Building out the input set before finalizing V1, in three stages.

| Stage | Scope | Status |
|-------|-------|--------|
| 1 | **Bucket-1 inputs** — `Email`, `Phone`, `URL`, `Time`, `Rating`, `Slider`, and `help=` text on every input | ✅ |
| 2 | **File uploads** — `File` (transient) and `S3File` (S3-persisted) input types with `@backend` file handles; `aws` connection type; multipart upload endpoint; per-field `max_size_mb` / `accept` | ✅ |
| 3 | **Sankey mapping input** — `inputs.Sankey`: a weighted many-to-many mapping; columns from a static list or a `steps` reference; click-to-connect with per-link weights; `normalize` (default True) enforces per-source sum-to-100; SVG ribbon diagram | ✅ |

## Noted follow-ups

- Operator-config template deps in the edit cascade — today an operator's templated config (e.g. a `TriggerDag(run_id='{{ steps.x.y }}')`) doesn't enter the dep graph. Editing `steps.x.y` cascades to any layout that reads it, but the operator itself isn't marked as depending on it, so a re-submit of the downstream step doesn't realize the operator's input has changed. The pre-edit limitation, surfaced here because multi-backend / chain processing makes the gap more visible. A fix: extend `_collect_deps` to scan operator config strings the way `Templated` strings are scanned elsewhere; the operator becomes a dep target that propagates through the cascade.

- Multi-backend display in the submission summary — the frontend's StepBlock shows one "returned" value per step, reading the legacy `step.backend_return` (which now carries the first backend's return as a representative). For multi-backend nodes this hides the rest. A proper fix iterates `step.external_state` and renders one row per chain step (each backend's return, each operator's state) — the data is all there, the display just needs an update.

- Multiple `@backend.branch` per chain — currently at most one branch backend is allowed *inside a node's `>>` chain* (standalone `@backend.branch` nodes are unaffected). Lifting this means resolving which branch's return drives downstream routing when several exist in the same chain — likely "the last branch backend wins" or "explicit chained routing." Worth designing once a real use case shows up.

- Conditional chain steps that skip work without leaving the node — `@backend.branch` today routes to *different downstream nodes* (the workflow forks), but there's no construct for "run this chain step only when a condition holds; otherwise pass-through to the next step in the same chain." Authors hit this whenever a chain has an optional transformation — e.g. "if the upload is .xlsx, normalize to CSV; if it's already .csv, skip the normalize step and use the raw key." Today the workaround is to put the conditional inside the backend body (the backend always runs, but its body short-circuits and returns the raw key unchanged) — which works but hides the conditional from the workflow graph and forces every chain consumer through one extra @backend call. A real fix would be a chain-level skip predicate — `@backend(when=lambda dataset: dataset['filename'].endswith('.xlsx'))` or `submit >> normalize.when(...) >> extract_codes()` — that the chain processor evaluates before invoking. Worth designing properly when a second use case shows up: file-type forks are common but not the only shape; report-conditional analysis (`only run kpi_totals when the dataset is non-empty`) and lazy-fetch (`only call the slow API when the user opted in`) want the same construct. Distinct from `@backend.branch`'s node-fork semantic — both belong in the framework but solve different problems.

- ✅ **Submission listing: paginated, filtered, sorted, soft-delete** — `GET /api/forms/{id}/submissions` is now server-paginated (`limit`/`offset` → `{submissions, total, limit, offset}` envelope), filters on state multi-select / substring search / current_step / `updated_since`–`updated_before` window / show-deleted toggle (admin), and accepts a multi-column sort spec (`?sort=col:dir,col:dir`) on every sortable column including the new `updated_at` ("last activity"). The form's Submissions tab uses URL-backed state for shareable views and survives refresh. Bulk soft-delete (admin) tombstones via `Submission.deleted_at` rather than removing rows; every user-facing read path filters `deleted_at IS NULL`. Replaces the prior client-side slice.

- ⬜ **Unify submission-listing and submission-export endpoints** — today `GET /api/forms/{id}/submissions` (session-auth, offset pagination, calendar-date window, multi-column sort) and `GET /api/export/submissions` (bearer-auth, keyset cursor, ISO-timestamp window, terminal-only mode) duplicate the date-window / paging / filter machinery on conceptually identical data. Plan: one endpoint that accepts either auth (session OR bearer), offers `limit/offset` OR `cursor` mode (mutually exclusive — 400 if both are passed), parses date-or-ISO for the window params, returns the matching envelope per mode (`{submissions, total, limit, offset}` for offset; `{submissions, next_cursor}` for cursor), supports the union of filters, and pins sort to `(updated_at, handle)` when in cursor mode (keyset requirement). Migration: ship the unified endpoint alongside the current two, point the UI at it, deprecate the export endpoint after a release cycle. Drop the `# TODO(unify-listing-and-export):` markers on both endpoints as the trail.

- Console theme polish — the admin console's own visual theme (the form summary page, tabs, drawer, listings) needs a design pass. The form-level Theme tab styles end-user forms but not the console itself. Doesn't block any feature; flagged so it doesn't get forgotten when polish time comes.

- Collapsible step-detail in the submission drawer — the drawer shows each step's captured values inline as a flat list. For long submissions with dense per-step data this gets noisy. Each step block could become a collapsible (closed by default once submitted, open while awaiting), with sub-content for nested views like XCom payloads or backend return values. Nested drawers were considered and rejected — they get recursive fast — so an in-place collapsible is the right shape. Deferred; the flat layout is functional for typical submissions today.

- Configurable poll rates — the submission-view polling now tiers automatically by workload kind (2s when an Airflow operator is in flight, 10s for pure human-input steps, 30s once terminal). The numbers are baked into `useSubmission.ts`. A real install may want them tunable — a Form-level override ("this one's chatty, slow it down"), an install-level default in admin settings, and/or user preferences ("I'm on a slow connection"). The right surface is open: probably a `polling` block in form theme/config plus an admin Settings page section. Distinct from the variables tool — these are UI behaviour knobs, not template values.

- Patch active submissions to the current form version — submissions are pinned to the `form_version_id` they started on, so a re-parse flows to *new* runs but not in-flight ones. The narrow escape hatch is built: `POST /api/forms/{id}/submissions/{id}/repin` (admin-only) re-pins a submission to the live version, validating shape compatibility against the submitted steps first — deleted node, missing field, type change, removed option, or now-missing button each block the re-pin with a 409 + diff; an empty diff updates the pin in place and records a `submission_repinned` event. The Submission Summary page shows a "Re-pin to v{N}" affordance when the submission lags the live version. ✅ for the narrow case. The broader auto-roll-forward — patches automatically flow to every active submission — is a deeper question (auto-apply policy, conflict resolution, user-visible notice) and remains deferred to its own design pass.

- Variables tool — a config/variables store, separate from the credential-only connection store. Lets a form author reference an install-scoped value (a bucket name, a webhook URL, a default region) via the same `{{ variables.x }}` templating they already use for `steps.x.y` and `{filename}`, instead of hardcoding strings or smuggling config into connections. Surfaces as a Variables admin page; resolves at runtime alongside the existing template namespaces. Distinct construct from connections (different page, different authorization, different lifecycle — variables are not sensitive in the same way credentials are). Triggered by the (now-fixed) credential/bucket conflation on the AWS connection; the broader pattern — "where do install-scoped non-secret values live" — wants its own design pass.

- Airflow dependency-aware clearing — when a frontflow edit clears Airflow task instances, frontflow clears at the granularity it can see: a whole DAG run for an affected `trigger_dag`, a single task instance for an affected sensor/xcom/HITL operator. It dedupes frontflow-visible overlap (a task-clear subsumed by a whole-run clear is dropped). What it cannot do without the DAG's task-dependency graph is clear a *truly minimal* set — two sensors referencing tasks in the same dependency subtree still clear redundantly, and Airflow's own `include_downstream` cascade is relied on rather than computed. Closing this needs frontflow to fetch and traverse the DAG structure via Airflow's API (or have the operator declare task edges in the DSL). Deferred — the current clearing is correct, just not provably minimal.

- Orphan upload-blob cleanup — substantially mitigated by deferred upload (file fields no longer call the upload endpoint at pick time, so abandoned picks never write a blob in the first place). Remaining cases: a `File` field's blob from a *successfully uploaded* submit that then never reaches a terminal state. A submission-terminate sweep and a periodic stale-blob purge would still tidy those — left on the roadmap, but the hot path is fixed.

- Pattern / glob folder grants — cross-tree matches like "all folders named marketing". Deferred from M2 (prefix grants shipped); needs its own design (glob syntax, an Access-UI preview of what a pattern matches) before building.

- ✅ **Persist `external_state`** — `external_state` is now written into the submission
  snapshot and restored on hydrate, so a triggered run's id survives a backend restart
  and the trigger never re-fires.
- ✅ **HITL response in `steps`** — `_poll_hitl` and the response endpoint record
  `chosen_options` / `params_input` into `external_state`, so a form can read
  `steps.<hitl id>.chosen_options`; a new `contains` condition operator lets
  `displays.When` test a list-valued field (a HITL choice, a multi-select).
- ✅ **`AirflowHitlBranch`** — a HITL operator that routes the form's own chain on the
  human's choice, via an option→node-id `routes` map.
- Airflow `/api/v2` paths verified against a live 3.1.8 instance: trigger, sensors, XCom,
  and the nested `hitlDetails` route all confirmed working end to end.

## Pre-Airflow backlog (carried over)

- ⬜ `needs_review` gate setting (`@form` / `@node` / `@page`)
- ⬜ Product-wide / console theming UI
- ⬜ Pages-as-groups in the workflow graph
- ⬜ Smaller theme follow-ups: structural headings from H1–H4 tokens; expose
  `tracking` / `nodeGap` / `scrollHeadroom`; contrast validation; rehype-raw scope
- ⬜ Larger future surfaces: embeddable iframe form; home / reporting screen
