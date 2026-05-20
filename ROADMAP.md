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

- Configurable poll rates — the submission-view polling now tiers automatically by workload kind (2s when an Airflow operator is in flight, 10s for pure human-input steps, 30s once terminal). The numbers are baked into `useSubmission.ts`. A real install may want them tunable — a Form-level override ("this one's chatty, slow it down"), an install-level default in admin settings, and/or user preferences ("I'm on a slow connection"). The right surface is open: probably a `polling` block in form theme/config plus an admin Settings page section. Distinct from the variables tool — these are UI behaviour knobs, not template values.

- Patch active submissions to the current form version — submissions are pinned to the `form_version_id` they started on, so a re-parse flows to *new* runs but not in-flight ones. The narrow escape hatch is built: `POST /api/forms/{id}/submissions/{id}/repin` (admin-only) re-pins a submission to the live version, validating shape compatibility against the submitted steps first — deleted node, missing field, type change, removed option, or now-missing button each block the re-pin with a 409 + diff; an empty diff updates the pin in place and records a `submission_repinned` event. The Submission Summary page shows a "Re-pin to v{N}" affordance when the submission lags the live version. ✅ for the narrow case. The broader auto-roll-forward — patches automatically flow to every active submission — is a deeper question (auto-apply policy, conflict resolution, user-visible notice) and remains deferred to its own design pass.

- Variables tool — a config/variables store, separate from the credential-only connection store. Lets a form author reference an install-scoped value (a bucket name, a webhook URL, a default region) via the same `{{ variables.x }}` templating they already use for `steps.x.y` and `{filename}`, instead of hardcoding strings or smuggling config into connections. Surfaces as a Variables admin page; resolves at runtime alongside the existing template namespaces. Distinct construct from connections (different page, different authorization, different lifecycle — variables are not sensitive in the same way credentials are). Triggered by the (now-fixed) credential/bucket conflation on the AWS connection; the broader pattern — "where do install-scoped non-secret values live" — wants its own design pass.

- Airflow dependency-aware clearing — when a frontflow edit clears Airflow task instances, frontflow clears at the granularity it can see: a whole DAG run for an affected `trigger_dag`, a single task instance for an affected sensor/xcom/HITL operator. It dedupes frontflow-visible overlap (a task-clear subsumed by a whole-run clear is dropped). What it cannot do without the DAG's task-dependency graph is clear a *truly minimal* set — two sensors referencing tasks in the same dependency subtree still clear redundantly, and Airflow's own `include_downstream` cascade is relied on rather than computed. Closing this needs frontflow to fetch and traverse the DAG structure via Airflow's API (or have the operator declare task edges in the DSL). Deferred — the current clearing is correct, just not provably minimal.

- Orphan upload-blob cleanup — a transient `File` upload that is never submitted leaves its blob in the `upload_blob` table forever. Blobs are tied to a submission on submit and could be cleaned when it terminates, plus a periodic sweep for never-submitted ones. Deferred from Stage 2 (the upload path is correct without it).

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
