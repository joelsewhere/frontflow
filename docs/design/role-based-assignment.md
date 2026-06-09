# Role-based assignment — design

Status: DRAFT v2 for review. Supersedes v1.
Audience: framework authors. Not user-facing.
Purpose: settle the model for role-based access, form-to-form
assignment, external user identity, picker inputs, notifications,
and signed-link embedding, before any code is written. Mark this
up.

## Changelog vs v1

Pinning what changed and why so reviewers don't re-litigate
settled points.

- **Vocabulary fixed.** Node = a section on a page (not a page;
  there can be multiple nodes per page). Submission = a shared
  workspace where multiple users contribute; no "creator" concept.
- **Picker decorators replace input classes.** `@users`,
  `@users.external`, `@users.email`, `@users.groups` — the
  decorator name encodes the identifier type; the body returns
  identifiers; frontflow handles label rendering.
- **`@role` decorator dropped.** Roles are declared as Role
  symbols at module scope and referenced inline on `@node` and
  per-input `role=`. The decorator's function body would have
  been dead weight.
- **Input-level read permissions dropped.** Per-input `role=`
  means **write** access for that role; read of all inputs on a
  node follows the node's read permissions. Hiding a written
  field's value doesn't make sense — split the node if you need
  that boundary.
- **Single parent per submission.** Column on submission row, no
  join table. Multi-parent scenarios are niche and deferred.
- **One submission per form, not per assignment.** A submission
  is a shared workspace; assignments determine who can act on
  which nodes. No more "Bob's submission of form X."
- **Re-grant inserts a new row, never reactivates.**
  `submission_assignment` is append-only; each grant window is
  its own row with `granted_at` / `revoked_at`. Historical
  windows are recoverable.
- **`revoked_by_user_id`** added to `submission_assignment` for
  audit completeness.
- **External user delete/edit endpoints** added. External system
  is source of truth; frontflow exposes endpoints for the
  external system to mark users inactive and update mapped
  attributes when they change there.
- **Notification hooks are per-form** via `@form(on_assigned=...,
  on_submitted=..., on_failed=..., on_revoked=...)`. No
  project-wide event registry. Cross-cutting concerns are a
  customer-side `@form` wrapper pattern.
- **Per-user notification preferences** stored on the User row;
  accessible to handlers; not enforced by frontflow itself.
- **Auth check defaults to allow** when no role gates apply.
  Forms without roles behave exactly like today.

## Why this exists

Today frontflow has three pieces of access control:

1. **Form visibility** — `public`, `unlisted`, `restricted`.
   Whole-form.
2. **FormACL** — explicit user grants on a restricted form.
   Whole-form.
3. **Folder grants** — admin-managed user-to-folder mappings,
   conferring access to every form in that folder. Bulk-grant
   convenience over (2).

This is sufficient for "an admin or a small ACL fills out forms
one user at a time." It does not handle the use case that
motivated this design:

> A manager submits a form. Their submission spawns downstream
> submissions of *other* forms, with specific people assigned to
> specific roles on those submissions. Multiple people contribute
> to the same submission. Some inputs are role-gated — the
> approver fills the decision field; the requester fills the
> rest, on the same node. The manager sees their submission AND
> the downstream submissions in one place.

Six new concepts, interlocking, are needed:

| Concept | One-sentence summary |
|---|---|
| **Role** | A named permission symbol — referenced by identity in form definitions and assigned to users at runtime. |
| **Permission template** | What a role can do at the node / input level. Declared in DSL, versioned with the form. |
| **Picker input** | A decorator-based input type producing a constrained list of identifiers (frontflow users, external users, emails, groups). |
| **External user identity** | A foreign-key mapping from frontflow's User row to the customer's source-of-truth identifier (the Canvas SIS-ID model). Pluggable. |
| **Assignment** | A per-submission, per-user, per-role record granting access to act on one submission as a role. |
| **Signed link** | A time-limited URL proving "the bearer of this URL is allowed to act on submission X as user Y." Used for notifications and filtered iframe embedding. |

Each is small on its own. The complexity is in how they fit.
The rest of this doc is that fit.

## Guiding principles

These constrain every decision below.

**P1. DSL is the source of truth for structure and policy.** A
form file declares its roles, its permission templates, and its
assignment operators. The admin UI manages **state** (who's
assigned to which role on which submission) and **operations**
(revoking, re-running, viewing). The admin UI never overrides
DSL-declared settings. This already applies to `private=True`,
`iframe_allowed_origins`, and `tags`; it now applies to roles
and permission templates.

**P2. Permissions are additive.** Explicit grants always grant,
never revoke. To remove access, remove the grant — not "add a
narrower permission." This matches how form visibility + folder
grants already compose.

**P3. Audit is permanent, revocation is instant.** Permission
templates are versioned with the form (immutable for in-flight
submissions; auditable forever). Per-submission user assignments
are timestamped, append-only (revocation takes effect at the
next request; the window is preserved in the row). The two
systems run in parallel; the runtime check combines them.

**P4. External identity is foreign-keyed, not replicated.** A
frontflow `User` row may carry an `external_id` referring to the
customer's source-of-truth identity system. The two systems
coexist. Frontflow does not become a user-management system; it
stays out of the way.

**P5. The tool must be usable without external services.** A
default in-app inbox at `/my-tasks` is the floor. Notification
handlers and external identity hooks are how authors *extend*
the system, not how authors are forced to use it.

**P6. The DSL is Python.** Roles, picker inputs, permission
templates, and assignments are real Python objects with real
APIs — not strings in a config file. Static analysis, IDE jump-
to-definition, and type checking all work.

**P7. Notification is per-form, not project-wide.** Cross-cutting
concerns are a customer-side pattern (wrap `@form` with defaults).
Frontflow provides the per-form mechanism.

## Vocabulary

Used precisely below.

- **Form** — the existing concept. A `@form`-decorated workflow.
- **Page** — a screen rendered to the user. Existing #18 work.
- **Node** — a section on a page. There can be multiple nodes
  per page. The existing concept.
- **Input** — a field within a node (`inputs.Text`, etc.).
- **Picker** — a special input type (`@users`, `@users.external`,
  `@users.email`, `@users.groups`) that produces an identifier —
  used to drive `Assign(to=...)`.
- **Submission** — a shared workspace for one traversal of one
  form. Pinned to a `form_version`. **Multiple users contribute
  to one submission**; no creator concept.
- **Role** — a named permission symbol, scoped to one form,
  declared in the form's source as `Role("name")`. Referenced by
  Python identity from nodes, inputs, and Assign operators.
- **Permission template** — the mapping from role → what that
  role can do. Declared in DSL alongside the form's structure.
  Versioned with the form (lives on the form_version snapshot).
- **Assignment** — a runtime row: "user U is assigned role R on
  submission S, granted at time T (revoked at time T+N if
  applicable)." Append-only.
- **External identity** — a string identifier from the customer's
  source-of-truth user system, mapped to a frontflow `User` via
  the `external_id` column. Resolvable through the
  `resolve_external_user` hook.
- **Signed link** — a URL with a cryptographically signed token
  proving "the bearer is allowed to act on submission X as user
  Y." Time-limited.

## 1. Role: the symbol

A `Role` is a first-class Python object declared at module scope
in a form file:

```python
from frontflow import Role, form, node, inputs

requester = Role("requester")
approver = Role("approver")

@form(form_id="hiring_request")
def hiring_request():
    ...
```

The string passed to `Role(...)` is its identifier — used in URLs,
admin UI, and audit logs. Must be unique within the form.

The Python *object* is what code references. Two roles with the
same identifier in different forms are different objects; they
do not share permissions or assignments. (Cross-form shared
roles are out of scope for v1.)

### 1.1 Referencing roles

Three shapes for declaring "who can do what":

**Single role on a node, defaults to write:**

```python
@node(role=approver)
def approval_decision():
    decision = inputs.Radio(label="Approve?", options=["Yes", "No"])
    return decision, Button("Submit")
```

`approver` has write on the node; users without the `approver`
role see this node as **pending** (see §4.4).

**Verb mapping on a node:**

```python
@node(role={"write": approver, "read": [monitor, requester]})
def approval_decision():
    ...
```

A dict maps verb → role(s). `write` permits filling and submitting;
`read` permits viewing the node's state but not interacting with
inputs. `read` is automatically granted to anyone in `write`.

**Per-input role (write only):**

```python
@node(role={"write": [requester, approver], "read": [requester, approver]})
def request_form():
    summary = inputs.Text(label="Summary", role=requester)
    decision = inputs.Radio(
        label="Approver decision",
        options=["Yes", "No"],
        role=approver,
    )
    return summary, decision, Button("Submit")
```

Per-input `role=` means write access for that role on that
specific input. **Read follows the node** — anyone with read on
the node sees every input's value. Hiding a written field from
people who can see the node doesn't make sense; if you need that
boundary, split the node.

### 1.2 Default role

A node with no `role=` declaration is fillable by **anyone with
form-level access** (per the existing `public` / `unlisted` /
`restricted` model). This preserves backwards compatibility — a
form with no role declarations behaves identically to today.

A form that wants every node to be explicitly role-gated declares
`@form(default_role=None)` — any node without `role=` becomes a
compile-time error.

## 2. Permission templates

The combined declaration — what roles exist, what they each do
at the node and input level — is the **permission template**.
Declared in DSL; versioned with the form_version snapshot.

When the form's DSL changes such that the serialized form is
different, a new `form_version` is minted. The permission
template is part of the serialization, so a permission change
spawns a new version. In-flight submissions stay pinned to the
form_version they began on, which means they stay pinned to the
permission template at the time of their creation.

**A permission change does not retroactively affect in-flight
submissions** unless the submission is repinned (existing
`repin_submission` operation). To retroactively grant access on
an existing submission, repin it to a newer version.

This is what makes audit + revocation coexist (P3):
- The historical answer "what could role R do on submission S at
  time T?" is read from the form_version snapshot.
- The current answer "can user U act as role R on submission S
  right now?" combines that snapshot with the live assignment
  state.

### 2.1 Versioning rules

A change to any of the following produces a new form_version:

- Adding, removing, or renaming a `Role`
- Changing any node's `role=`
- Changing any input's `role=`

Same content_hash mechanism as today.

### 2.2 Backward compatibility

Forms without roles produce a permission template with the
implicit "default" role only, no per-node or per-input gates.
The runtime check short-circuits to existing visibility-only
checks. **No existing form changes behavior.**

## 3. External user identity

Customers with their own user system (Canvas LMS, internal HR,
Auth0, etc.) need to keep frontflow's user table aligned with
theirs.

**Model:** the frontflow `User` row carries an optional
`external_id` column — nullable, unique-when-set, indexed.

| frontflow `User` row | `external_id` | Meaning |
|---|---|---|
| Exists, external_id set | `"u-123"` | Linked to external system |
| Exists, external_id null | (n/a) | frontflow-only user (admin, etc.) |
| Does not exist | (n/a) | External user not yet touched frontflow |

The third case is the interesting one. A customer with 10,000
users in their LMS doesn't pre-populate 10,000 frontflow rows.
Users are resolved on first touch via a pluggable hook:

```python
@frontflow.resolve_external_user
def resolve(external_id: str) -> User | None:
    record = customer_user_system.find(external_id)
    if record is None:
        return None
    return User(
        username=record.username,
        email=record.email,
        external_id=external_id,
        is_admin=False,
    )
```

Called once per (request, external_id) pair; the created User row
is then permanent until manually deleted or marked inactive. The
hook is responsible for validation — it's the customer's chance
to verify the external_id against their own system before
frontflow creates the row.

**Default behavior (no hook registered):** frontflow refuses to
auto-resolve external IDs. The customer either registers a hook,
pre-populates User rows manually, or doesn't use external
identity at all.

### 3.1 External delete + edit endpoints

When the customer's user system deactivates a user, frontflow
should react. New admin-authenticated endpoints:

```
DELETE /api/users/external/{external_id}
PUT    /api/users/external/{external_id}
GET    /api/users/external/{external_id}
```

`DELETE` marks the User row inactive (preserves audit; doesn't
hard-delete) and revokes all active assignments for that user.
Each revoked assignment gets a `revoked_at=now()` row insertion
with `revoked_by_user_id` = the system actor for the API call.

`PUT` updates mapped attributes (username, email). The
`external_id` itself is not editable — changing external_id is
done by `DELETE` the old + `POST` (create) the new.

`GET` returns the current state — useful for the customer's
system to verify sync.

These endpoints are intended to be called by the customer's user
system (webhook on user deactivation, etc.). Authentication is
admin-credential or a dedicated system token; not exposed to
end users.

### 3.2 Per-user notification preferences

A new `notification_preferences` column on the User row stores
the user's opt-out state per channel:

```json
{
  "email": true,
  "slack": false,
  "in_app": true
}
```

Channels are open-ended strings — frontflow doesn't enforce a
fixed set. The customer's `on_assigned` hook reads this and
respects it:

```python
def on_assigned(event):
    prefs = event.assignee.notification_preferences
    if prefs.get("slack", True):
        send_slack(event.assignee, event.signed_link)
    if prefs.get("email", True):
        send_email(event.assignee, event.signed_link)
```

Frontflow exposes `/api/users/me/notification-preferences` (the
user updates their own) and `/api/users/{id}/notification-preferences`
(admin sets for any user). The in-app inbox at `/my-tasks` is
not gated by notification preferences — it's the floor (P5).

### 3.3 What this does NOT do

- Does not turn frontflow into a user-management system.
- Does not require customers to use external identity.
- Does not synchronize user attributes proactively from the
  external system.

## 4. Picker inputs

`Assign(to=...)` needs to reference an input that produces an
identifier. Free-text fields are footguns — typos resolve to
nothing, deactivated users still appear. Pickers are constrained
dropdowns whose options are computed server-side by a developer-
provided resolver.

### 4.1 The decorator namespace

Top-level import `users` is the entry point:

```python
from frontflow import users
```

Four picker decorators in v1:

| Decorator | Resolver returns | Resolved to User via |
|---|---|---|
| `@users` | `list[int]` — frontflow user_ids | direct DB lookup |
| `@users.external` | `list[str]` — external IDs | `resolve_external_user` hook |
| `@users.email` | `list[str]` — email addresses | match by email; create stub User if no match |
| `@users.groups` | `list[int]` — frontflow group_ids | direct DB lookup |

The decorator name encodes the identifier type. The resolver
function returns **just identifiers** — frontflow handles label
rendering itself by looking up the User/Group record (or calling
the external resolve hook for `@users.external`).

### 4.2 Resolver signature

```python
@users(label="Recruiter")
def recruiter(ctx):
    """`ctx` is a request-scoped context object carrying:
      - ctx.user        — the user filling out the form (or None)
      - ctx.form_id     — the form_id of the form being filled
      - ctx.submission  — the submission instance (None if not yet started)
      - ctx.steps       — upstream node values (for cascading dropdowns)
    """
    return [42, 43, 44]   # frontflow user_ids
```

The resolver runs **server-side at render time** — every page
render fires the resolver, reflecting latest state. Customers
who need caching wrap with their own memoization (TTL,
functools.lru_cache, etc.). No `cache=` kwarg in v1.

`ctx.steps` lets cascading work — "show only recruiters in the
department picked above":

```python
@users(label="Recruiter for this department")
def recruiter(ctx):
    dept = ctx.steps.start.department
    return [u.id for u in fetch_recruiters() if u.department == dept]
```

### 4.3 Empty-body shortcut

For "just list all frontflow users / groups," the body is a stub:

```python
@users(label="Recruiter")
def recruiter(ctx): ...

@users.groups(label="Team")
def team(ctx): ...
```

Empty body (`pass` or `...`) → decorator substitutes a built-in
resolver querying the relevant table directly.

`@users.external` and `@users.email` have no built-in default
(those depend on customer-side systems); empty body there is a
**compile-time error**: "no default resolver for @users.external;
provide a function body."

### 4.4 Decorator kwargs

```python
@users(
    label="...",
    id="...",                    # input id (defaults to function name)
    required=False,
    multi=False,                 # single vs multi select
    default=None,
    help_text="",
)
def my_picker(ctx): ...
```

All four picker decorators take the same kwargs.

### 4.5 Multi-select

`multi=True` produces a multi-select. The resolver's return shape
is unchanged (it's the *available options*); the input's *value*
is a list:

```python
@users.external(label="Reviewers", multi=True)
def reviewers(ctx):
    return ["sis-001", "sis-002", "sis-003"]
```

When the user picks two, the input's value is `["sis-001",
"sis-002"]`. `Assign(to=steps.start.reviewers)` fans out — one
assignment per picked identifier.

### 4.6 Per-use overrides

The module-level decorated object can be called with overrides:

```python
@node
def kickoff():
    return recruiter, Button("Submit")

@node
def emergency_kickoff():
    return recruiter(required=True, label="Recruiter (required)"), Button("Submit")
```

Calling produces a derived Input with overrides applied; resolver
is unchanged.

### 4.7 Picker validation by Assign

`Assign(to=...)` reads which picker decorator was used (compile
time):

- References a `@users` picker → OK; runtime resolves user_ids
  directly.
- References a `@users.external` picker → OK; runtime calls the
  resolve hook per identifier.
- References a `@users.email` picker → OK; runtime matches by
  email, creates a stub User row if no match (the invite-by-email
  pattern). Stub users have only `email` set; `external_id` and
  `username` are null; they get assignment access via signed
  links.
- References a `@users.groups` picker → OK; runtime expands group
  membership at execution time. One assignment per member.
- References a free-text input (`inputs.Text`, `inputs.Email`) →
  **compile-time error**: "Assign requires a picker; got a text
  input. Use @users.email if you want to assign by email."

### 4.8 What about `inputs.Email` for non-assignment use?

`inputs.Email` and other free-text inputs stay — they're still
the right shape for "what's your contact email" or "where should
we send the receipt." The compile-time check is specifically on
`Assign(to=...)`; everywhere else, free-text fields work as
today.

## 5. Assignment

The `Assign` operator creates per-submission grants — "user U
has role R on submission S of form F." One operator call per
assignment, dispatched from a node body:

```python
from frontflow import Assign, users

@users(label="Recruiter")
def recruiter(ctx): ...

@node
def kickoff():
    project = inputs.Text(label="Project name", required=True)
    submit = Button("Kick off")

    spawn_screening = Assign(
        form="hiring_screening",
        to=steps.kickoff.recruiter,
        role="recruiter",
        prefill={"project": steps.kickoff.project},
    )
    submit >> spawn_screening
    return project, recruiter, submit
```

### 5.1 Data model

Two new tables, plus a column on `submission`:

```
submission
    ...existing columns...
    parent_submission_id     FK → submission (nullable, indexed)
    parent_assign_node_id    string (nullable)
    parent_assign_op_idx     int (nullable)

submission_assignment
    id                       PK
    submission_id            FK → submission     (indexed)
    user_id                  FK → user           (indexed)
    role_id                  string              # role identifier on the form
    granted_at               datetime
    granted_by_user_id       FK → user
    granted_by_submission_id FK → submission     (nullable; null = manual admin grant)
    revoked_at               datetime            (null = currently active)
    revoked_by_user_id       FK → user           (nullable)

user_group
    id                       PK
    name                     string
    external_id              string              (nullable, unique-when-set)
    created_at               datetime

user_group_membership
    user_id                  FK → user
    group_id                 FK → user_group
    added_at                 datetime
    added_by_user_id         FK → user
    (composite PK on user_id, group_id)
```

`submission_assignment` is **append-only**. A regrant after
revocation inserts a NEW row — never reactivates an old one.
Querying "is user U currently assigned R on S" is "is there a
row where `revoked_at IS NULL` for that triple." Historical
windows are the full row list ordered by `granted_at`.

`submission.parent_*` columns capture the single-parent
relationship — a submission has at most one parent submission
(created by a specific Assign operator at a specific node).
Multi-parent submissions are not in v1.

Index on `submission_assignment(submission_id, user_id, revoked_at)`
makes the auth check (§8) O(1).

### 5.2 `Assign` semantics

`Assign(form=..., to=..., role=..., prefill=...)`:

- **`form`** — form_id of the child form. Validated at compile
  time against the form registry; Assign to an unknown form fails
  workflow loading.
- **`to`** — must reference a picker input. Compile-time error
  if a non-picker is passed (see §4.7).
- **`role`** — string matching a Role identifier declared in
  the child form. Validated at compile time.
- **`prefill`** — dict of input values pre-populated on the
  child submission. Values can be literals, step references, or
  Jinja templates (same engine as `displays.Markdown`).

When Assign executes:

1. Resolve `to` to one or more concrete User rows using the
   picker's identifier type (frontflow user_id → direct lookup;
   external_id → resolve hook; email → match-or-create-stub;
   group_id → expand to member users).
2. For each user, find or create the child submission:
   - If a child submission for this parent + Assign node + role
     combo already exists, reuse it (idempotent re-execution).
   - Otherwise, create a new submission of the child form,
     pinned to its current form_version, populated with `prefill`.
   - Set `parent_submission_id`, `parent_assign_node_id`,
     `parent_assign_op_idx` on the child.
3. Insert a `submission_assignment` row (user, role, child
   submission). If a previous assignment for that triple exists
   with `revoked_at IS NULL`, the new insert is a no-op
   (idempotent); if it exists with `revoked_at` set, a new row
   is inserted (re-grant).
4. Trigger the `on_assigned` notification handler (§6) for the
   form, if registered.

Atomic per assignee — a multi-user `to=` is N calls (N
assignments succeed-or-fail independently).

### 5.3 Cardinality

A single Assign call creates one assignment per identifier in
`to=`. Multi-select pickers fan out. Different forms / different
roles → multiple Assign calls:

```python
legal = Assign(form="legal_review", to=steps.s.legal_lead, role="reviewer")
finance = Assign(form="finance_review", to=steps.s.finance_lead, role="approver")
ops = Assign(form="ops_signoff", to=steps.s.ops_team, role="signer")  # multi
submit >> [legal, finance, ops]
```

### 5.4 Pending render state

A user with access to a submission but **no role permitting
write** on a specific node sees the node in **pending state**:

> This step is assigned to <role_label>. You'll be able to act
> on it when your colleague completes their part.

Distinct from a 404 (the node exists; the user just can't write
yet) and from a hard error (this is normal flow). Inputs render
disabled; no submit button.

Once the user's role permits write (either because they're newly
assigned, or because the upstream completed and the node's read
gate widens), the node renders normally on the next request.

### 5.5 Edit cascade extends to assignments

When a parent submission's answer is edited via the existing
edit-cascade mechanism, downstream state is normally cleared.
With assignments, this extends:

- Editing an input that **fed an Assign's `to=`** clears the
  downstream assignment and the child submission.
- Editing an input that fed `prefill` clears the child
  submission's state but preserves the assignment.
- The "change in isolation" escape hatch on the assignee field
  preserves both the existing assignment and the child
  submission — useful for "actually, reassign to Carol" without
  losing Bob's prior progress.

The cascade-clear vs preserve-downstream choice is a per-field
UI toggle in the edit modal. Default: cascade-clear (matches
today's behavior for non-assignment inputs).

## 6. Notifications

Per-form hooks declared on `@form`:

```python
@form(
    form_id="hiring",
    on_assigned=notify_slack,
    on_submitted=archive_to_drive,
    on_failed=alert_admin,
    on_revoked=notify_revocation,
)
def hiring():
    ...
```

Each is an optional callable; absent → no notification for that
event on that form. Cross-cutting concerns are a customer-side
wrapper (see §6.4).

### 6.1 Event slots

| Slot | Fires when |
|---|---|
| `on_assigned` | An Assign operator creates a new assignment on this form. One call per assignment. |
| `on_submitted` | A submission of this form reaches a terminal state. |
| `on_failed` | A backend node on this form errors. |
| `on_revoked` | An assignment on this form is revoked (admin, external system, or edit cascade). |

### 6.2 Handler signature

```python
def on_assigned(event):
    """`event` is a uniform payload across all event types.
    Fields vary by `event.kind`; common fields:
      - event.kind           — "assigned" | "submitted" | "failed" | "revoked"
      - event.submission     — the submission affected
      - event.form_id        — convenience
      - event.timestamp      — when it happened
    For "assigned":
      - event.assignee       — User row of the new assignee
      - event.role_id        — role identifier
      - event.signed_link    — URL with embedded auth token for this assignment
    For "revoked":
      - event.assignee       — User row whose access was revoked
      - event.role_id
      - event.revoked_by     — User row of who revoked
    """
    send_slack(event.assignee, event.signed_link)
```

### 6.3 Execution semantics

- Called **synchronously after the relevant state change is
  persisted.** The persistence has already happened; the
  handler is best-effort.
- Hook failures do NOT roll back the state change. Failure is
  logged and flagged on the relevant row (`notification_failed`
  flag on the assignment, etc.); admin UI has a "retry" button.
- Multiple handlers per slot are not supported in v1 — register
  one function per slot. (If a customer wants multi-handler,
  their function dispatches.)

### 6.4 Cross-cutting via wrapper

For project-wide defaults, the customer wraps `@form`:

```python
# customer_project/form.py
from frontflow import form as _ff_form

def form(**kwargs):
    """Project-wide @form wrapper with notification defaults."""
    kwargs.setdefault("on_assigned", notify_slack)
    kwargs.setdefault("on_failed", alert_admin)
    return _ff_form(**kwargs)
```

Then form files import this wrapped `form` instead of frontflow's
own. Per-form overrides via kwargs still work. **Frontflow does
not ship this wrapper.**

### 6.5 The `/my-tasks` in-app inbox

Always written, regardless of registered handlers. For a
signed-in user, lists every active assignment across every
form, ordered by `granted_at` desc:

```
My tasks (3)

  - Q3 Budget Review (legal review)        assigned 2 days ago
    Project: Acme acquisition                      [open →]

  - New hire: Sara Patel (offer approval)  assigned 5 days ago
                                                   [open →]
```

Filter and search are V2. Once a user's assignment ends (the
node they were assigned to is complete, or they're revoked),
the entry drops from the active list. "Past tasks" view is V2.

### 6.6 Per-user opt-out

Per-user notification preferences (§3.2) gate the customer's
own hooks — handlers read `event.assignee.notification_preferences`
and respect them. The in-app inbox is not gated.

## 7. Signed links

A signed link grants access to one specific submission as one
specific user, without requiring frontflow login. Used for:

1. Notification delivery (the URL in the Slack/email message).
2. Filtered iframe embedding (§8).

### 7.1 Structure

```
https://forms.example.com/forms/<form_id>/form/submission/<handle>?token=<signed_token>
```

The `signed_token` is a JWT-shaped envelope:

```json
{
  "user_id": 42,
  "submission_id": 1234,
  "scope": "fill" | "read",
  "exp": 1735000000,
  "iat": 1734900000,
  "issuer": "assign_operator" | "admin" | "embed"
}
```

Signed with the install's `FRONTFLOW_SECRET_KEY` (HS256). Opaque
to the recipient.

### 7.2 Verification flow

On a request with a `token` query parameter:

1. Verify signature. Invalid → 404 (don't leak validity).
2. Verify `exp > now`. Expired → 404 with "this link has
   expired" page.
3. Verify `submission_id` matches the URL. Mismatch → 404.
4. Verify `user_id` is active AND has at least one active
   `submission_assignment` for the submission. Revoked → 404
   with "access revoked" page.
5. Act as if `user_id` is signed in for this request only. No
   persistent session.

### 7.3 Scope and lifetime

- **Default lifetime:** 7 days from issuance. Per-Assign
  override: `Assign(..., link_ttl_days=30)`.
- **Scope:** `fill` (can write) or `read` (can view). Derived
  from the role's permissions at issuance.
- **Single submission only.** A user with three assignments
  gets three distinct tokens.
- **Revocation is instant.** The auth check reads live
  assignment state on every request.

### 7.4 What signed links are NOT

- Not a session cookie.
- Not a substitute for login.
- Not transferable (forwarding the URL forwards the access;
  same trust model as any URL-bearing-credential).

## 8. The runtime auth check

For a request from user U on submission S of form F, for node N
(and optionally input I):

1. **Resolve U.** From session cookie, signed link token, or
   external identity hook. None → anonymous.
2. **Form-level access.** Per existing visibility model (`public`
   reaches everyone, `unlisted` requires a token, `restricted`
   requires admin/ACL/folder grant). No → 404.
3. **Submission-level access.** Three cases:
   - The form declares no roles → submission is accessible to
     anyone with form-level access. (Default-allow per the v2
     correction.)
   - U has an admin grant → allow.
   - U has an active `submission_assignment` row for S (any
     role) → allow.
   - Otherwise → 404.
4. **Node-level access.** Read the form_version's permission
   template; combine with U's role assignments on S:
   - If the node has no `role=` declaration → allow (read +
     write, anyone with submission access).
   - If U has a role permitting `write` on N → allow write.
   - If U has a role permitting `read` on N → allow read; show
     pending state for write actions (§5.4).
   - Otherwise → render pending state with the role label.
5. **Input-level access (if I is specified):**
   - If I has no `role=` → allow per node-level result.
   - If U has a role matching I's `role=` → allow write.
   - Otherwise → input renders disabled; submission of the input
     is blocked.

The check is bounded: one form_version snapshot read (cached),
one assignment lookup with the (submission_id, user_id) index,
one local combination. No N+1.

## 9. Iframe extension

Today's iframe embed (#20 v1) renders public forms allowlisted
per origin. This work extends to **filtered embedding by external
user identity**.

A host page on `portal.company.com` embeds:

```html
<iframe src="https://forms.example.com/embed/my-tasks?token=<signed_token>" ...></iframe>
```

The signed token identifies a specific external user (via
`external_id`). The iframe renders that user's `/my-tasks` view
— their assignments only, embedded mode, no chrome.

Requires:

1. `/embed/my-tasks` route accepting a signed token (same
   structure as §7 but `scope="my_tasks"`, no `submission_id`).
2. Existing CSP `frame-ancestors` enforcement applies — the host
   page's origin must be on an install-wide embed allowlist.

Authenticated full-form embedding (not just `/my-tasks`) is a
follow-on; the mechanism is the same.

## 10. Migration

**Forms without roles** behave identically to today. Default
role catches everyone with form-level access; no gates apply;
no pending state; no Assign.

**Existing submissions** have no `submission_assignment` rows.
The auth check short-circuits at step 3 to "no roles declared →
allow."

**FormACL stays.** Roles + assignments are additive (P2).
A form can use folder grants, FormACL, AND roles simultaneously.

### 10.1 Phased rollout

1. **Phase 1** — `Role` symbol, `@node(role=...)`, per-input
   `role=`, permission-template snapshot on form_version, runtime
   gate. No Assign yet. (~2 weeks)
2. **Phase 2** — `external_id` column, `resolve_external_user`
   hook, external user delete/edit endpoints, notification
   preferences column. Parallel to Phase 1. (~1 week)
3. **Phase 3** — Picker decorators (`@users`, `@users.external`,
   `@users.email`, `@users.groups`), user_group table, group
   membership management. Depends on Phase 2. (~1.5 weeks)
4. **Phase 4** — `Assign` operator, submission_assignment table,
   parent-child columns on submission, parent-child UI on
   submission detail page, on_assigned hook, /my-tasks inbox.
   Depends on Phases 1, 3. (~2-3 weeks)
5. **Phase 5** — Remaining notification hooks (on_submitted,
   on_failed, on_revoked) + signed-link infra. Depends on
   Phase 4. (~1-2 weeks)
6. **Phase 6** — Iframe `/embed/my-tasks` route. Depends on
   Phases 2, 4, 5. (~1 week)

Total: ~8-10 weeks of focused work. Each phase ships
independently with its own tests, documentation, and a bundled
example. Backwards compatibility is a hard requirement at each
step.

## 11. What this doc settles

If you sign off on v2:

- The `Role` symbol model and three reference shapes (single,
  verb-mapped, per-input write-only).
- Permission-template-versioned-with-form, additive permissions,
  audit-via-version + revocation-via-append-only-timestamps.
- Canvas-SIS-id external identity, hook-based resolution,
  external delete/edit endpoints.
- Picker decorators (`@users`, `@users.external`, `@users.email`,
  `@users.groups`) with empty-body shortcut for built-ins.
- `Assign` operator: one call per assignment, list values fan
  out, prefill is a dict, single-parent submission relationship.
- `/my-tasks` inbox as the floor.
- Per-form notification hooks (no project-wide registry).
- Per-user notification preferences as a hook-read field, not
  framework-enforced.
- Signed link mechanism + verification flow.
- Phased migration plan.

## 12. What this doc does NOT settle

- Exact wire format for the form_version permission template.
- Visual design of `/my-tasks`.
- Admin UI specifics (revoke button placement, bulk revoke).
- Filter and search affordances on `/my-tasks`.
- Whether `prefill` supports computed values beyond step
  references.
- Group nesting (groups containing groups).
- Pagination + search-as-you-type for picker dropdowns (v2 when
  someone hits the wall on a 10k-user list).

## 13. Open questions (parked for later)

Per locked calls from review:

| # | Question | v1 call |
|---|---|---|
| 1 | Cross-form shared roles | Deferred; per-user/group assignment tracking matters more, and is doable with current data model |
| 2 | Role hierarchies / inheritance | Not v1 |
| 3 | Time-bounded assignments | Not v1 (good idea) |
| 4 | Bulk CSV assignment | Not v1 (good idea) |
| 5 | `*` / `None` permission sentinels | Not v1; explicit lists only |
| 6 | Per-submission-step role overrides | Not v1 |
| 7 | External identity drift / merge | Not v1 |
| 8 | Notification deduplication | Handler's responsibility (locked) |
| 9 | Signed-link rotation on key rotation | Accept invalidation (locked) |
| 10 | Auth-check perf | Index on `(submission_id, user_id, revoked_at)` ships in Phase 4 (locked) |

---

## Appendix A: Worked example

A hiring workflow, end-to-end. Two forms; one submission per
form; multiple contributors per submission via assignments.

```python
from frontflow import form, node, inputs, displays, users, Role, Assign, Button, steps

# Roles per form.
requester = Role("requester")

# Pickers, declared at module scope.
@users(label="Recruiter to handle this")
def recruiter(ctx): ...   # zero-config; uses frontflow's User table

@form(form_id="hiring_request", private=True,
      on_assigned=notify_via_slack,
      on_submitted=archive_request)
def hiring_request():

    @node(role=requester)
    def request():
        role_title = inputs.Text(label="Role to hire for", required=True)
        manager_email = inputs.Email(label="Hiring manager's email")
        return role_title, manager_email, Button("Submit request")

    @node
    def assign_team():
        # Default role on this node: anyone with form-level access.
        submit = Button("Assign recruiter")

        screening = Assign(
            form="hiring_screening",
            to=steps.assign_team.recruiter,
            role="recruiter",
            prefill={
                "role_title": steps.request.role_title,
                "manager_email": steps.request.manager_email,
            },
        )
        submit >> screening
        return recruiter, submit

    request() >> assign_team()


# Second form; declares two roles for its multi-contributor flow.
recruiter_role = Role("recruiter")
manager_role = Role("manager")

@form(form_id="hiring_screening", private=True,
      on_assigned=notify_via_slack)
def hiring_screening():

    @node(role=recruiter_role)
    def screening():
        candidates = inputs.TextBlock(label="Candidate notes")
        shortlist = inputs.MultiSelect(label="Shortlist", options=[...])
        return candidates, shortlist, Button("Send to manager")

    @node(role=manager_role)
    def interview():
        feedback = inputs.TextBlock(label="Interview feedback")
        decision = inputs.Radio(
            label="Decision",
            options=["Hire", "No hire", "Continue"],
        )
        return feedback, decision, Button("Submit")

    # Screening submission spawns the manager's assignment as a
    # second role on the SAME submission (not a new submission).
    # This uses a different mechanism — not Assign (which spawns
    # downstream submissions) but role transfer via the manager
    # email captured upstream. Modeled as a backend operator:
    #
    #   assign_manager_to_self(role="manager",
    #                          to=steps.screening.manager_email)
    #
    # (Detail of this mechanism is a Phase 4 design decision.)

    screening() >> interview()


hiring_request()
hiring_screening()
```

Runtime narration:

1. **Alice (requester)** opens `hiring_request` (she's in the
   ACL via folder grant). She fills the `request` node, then the
   `assign_team` node, picking Bob from the recruiter dropdown.
2. **On Alice's submit** of `assign_team`, the Assign operator
   fires. A new submission of `hiring_screening` is created,
   pinned to the current form_version, prefilled with the role
   title and Alice's email. A `submission_assignment` row is
   inserted (Bob, role `recruiter`, the new submission, granted
   by Alice, granted via the parent submission).
3. **Slack notifies Bob** (via Alice's form's `on_assigned`
   hook) with a signed link to his assignment. The signed token
   carries Bob's user_id and the submission_id.
4. **Bob clicks the link.** The token is verified; Bob acts as
   himself on the screening submission. He sees the `screening`
   node, where he has write. The `interview` node renders as
   pending — assigned to `manager` role, which Bob doesn't have.
5. **Bob submits.** A backend operator (the "assign manager to
   self" mechanism above) creates a second assignment on the
   SAME submission: Alice with role `manager`. Alice gets her
   own notification.
6. **Alice opens her `/my-tasks`** and sees the pending
   interview node. She fills it, submits. The submission
   completes; `on_submitted` fires.
7. **Alice's parent submission** (`hiring_request`) shows in its
   detail page: her own answers + the downstream
   `hiring_screening` submission, with its current state and a
   summary of who-acted-when.

Audit on either submission shows the form_version each ran on,
every assignment grant + revoke, every state transition, every
input change with user_id and timestamp.

If Alice removes Bob from his role mid-flight (admin action on
the assignment row), Bob's next request 404s. The audit row
preserves his access window from `granted_at` to `revoked_at`.

