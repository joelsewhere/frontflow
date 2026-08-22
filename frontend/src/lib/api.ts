/**
 * API client. Single fetch wrapper so error handling and base URL live in
 * one place. Endpoints are typed at the call site.
 *
 * The backend ships each node as a **layout tree** — a nested tree of
 * typed blocks (containers, display blocks, inputs, buttons). The
 * frontend renders it with a recursive component registry (see
 * components/blocks/). Structured data, never HTML.
 */

import { type Theme } from "../theme/theme";

// The API is served under /api (same origin as the bundled web UI).
// In local dev, point VITE_API_URL at the dev server, e.g.
// "http://localhost:8000/api".
const BASE_URL = import.meta.env.VITE_API_URL ?? "/api";

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

// Requests that take longer than this are treated as a dead/unreachable
// backend — nothing in this app legitimately runs this long.
const REQUEST_TIMEOUT_MS = 15_000;

// The unlisted-link token, if the form-filling UI was opened via an
// unlisted share link (/forms/:id/form?key=<token>). When set, it is
// appended as `?key=` to every API request so the backend's
// visibility check accepts the unlisted form. The fill pages call
// setUnlistedKey() from the URL on mount.
let _unlistedKey: string | null = null;

/** Record the unlisted-link token for subsequent API calls. */
export function setUnlistedKey(key: string | null): void {
  _unlistedKey = key && key.length > 0 ? key : null;
}

/** The unlisted-link token currently in effect, if any. */
export function getUnlistedKey(): string | null {
  return _unlistedKey;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  // Append the unlisted-link token, if one is in effect. The backend
  // reads it as the `key` query param for its visibility check; it is
  // harmless on routes that don't consult it.
  let url = `${BASE_URL}${path}`;
  if (_unlistedKey) {
    url += (path.includes("?") ? "&" : "?") + `key=${encodeURIComponent(_unlistedKey)}`;
  }

  let res: Response;
  try {
    res = await fetch(url, {
      ...init,
      signal: controller.signal,
      // Send the session cookie — needed for the admin console, and
      // for cross-origin dev (UI and API on different ports).
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        ...(init?.headers ?? {}),
      },
    });
  } catch (err) {
    // Network failure, or our own timeout aborted the request. Either
    // way the backend is unreachable — surface it as an ApiError so the
    // caller shows an error instead of hanging on a loading state.
    const aborted = err instanceof DOMException && err.name === "AbortError";
    throw new ApiError(
      aborted
        ? "Request timed out — is the backend running?"
        : "Couldn't reach the backend — is it running?",
      0,
    );
  } finally {
    clearTimeout(timer);
  }

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      // response wasn't JSON; keep statusText
    }
    throw new ApiError(detail, res.status);
  }

  return res.json() as Promise<T>;
}

// ---- Layout tree -----------------------------------------------------------

/**
 * One node in a layout tree. Containers carry `children`; display
 * blocks / inputs / buttons are leaves. `type` selects the React
 * component from the block registry; `props` are type-specific.
 */
export interface Block {
  type: string;
  id: string | null;
  props: Record<string, unknown>;
  children: Block[];
}

/**
 * Field-schema shape consumed by the widget bundle interface
 * (components/widgets/). Not an API response type — synthesized on the
 * frontend from a histogram_widget block's props.
 */
export type StepFieldType =
  | "text"
  | "number"
  | "select"
  | "textarea"
  | "checkbox"
  | "widget";

export interface StepField {
  name: string;
  label: string;
  type: StepFieldType;
  required: boolean;
  options: string[];
  placeholder: string;
  default: string | number | boolean | null;
  widget: string;
  widget_data: Record<string, unknown>;
}

// ---- Submission ------------------------------------------------------------

/** How an upstream edit left a step. "unaffected" for untouched steps. */
export type CascadeStatus = "unaffected" | "needs_review" | "needs_input";

/** The human-in-the-loop action an Airflow HITL task is waiting on. */
export interface HitlPrompt {
  subject: string | null;
  body: string | null;
  options: string[];
  /** Airflow's param schema for the action — rendered as extra fields. */
  params: Record<string, unknown>;
  defaults: string[];
  multiple: boolean;
}

export interface TaskInstance {
  task_id: string;
  state: string;
  is_hitl: boolean;
  /** Set only on a `superset.RefreshDashboard` step — the refresh it
   *  requested at this point in the chain. */
  dashboard_refresh?: DashboardRefresh | null;
  kind: "hitl" | "external" | "backend";
  /** Presentational backend-step grouping (`with backend_group(...)`)
   *  — consecutive backend tasks sharing group_id render as one
   *  collapsed status node titled group_title. */
  group_id?: string | null;
  group_title?: string | null;
  /** Edit-cascade status — how an upstream edit left this step. */
  status: CascadeStatus;
  /** The page this task belongs to — null for top-level nodes and
   *  backend steps. The chain is grouped into views by this. */
  page_id: string | null;
  page_title: string | null;
  /** Present only on an Airflow HITL task that is awaiting a response —
   *  the prompt the form renders to collect the user's answer. */
  hitl: HitlPrompt | null;
  /** A human-readable note — for an Airflow operator, the failure
   *  reason or a short status message. Null when there's nothing. */
  detail: string | null;
  /** Developer-supplied message rendered in the status panel while
   *  the operator is in a non-terminal state. Templated against the
   *  form's `steps` namespace and resolved on each advance. Null when
   *  no `waiting_message` was set on the operator. */
  waiting_message: string | null;
  /** Whether this step may be rerun on its own from the UI. When
   *  false, the per-step rerun menu is suppressed. */
  retryable: boolean;
  /** Per-operator polling rate hint (ms). useSubmission takes the
   *  minimum across all in-flight external tasks as the refetch
   *  interval — an operator that wants slower polling can declare
   *  it. Null → operator opts into the framework default. Always
   *  null for HITL and backend tasks (they don't drive polling). */
  poll_interval_ms: number | null;
  /** Per-step error message. Set when the step's backend (or chain)
   *  raised — `state` is "failed" in that case. Surfaced in the
   *  chain UI so the user sees the actual exception text, not just
   *  the bare "failed" label. */
  error: string | null;
  /** Full Python traceback paired with `error`. The chain UI shows
   *  this in a collapsible <details> panel below the short message. */
  traceback: string | null;
  /** Children spawned from this task by an Assign operator. Empty for
   *  tasks that didn't fire any Assign, or whose Assigns produced no
   *  grants. Each entry is one SubmissionAssignment row joined with the
   *  parent submission for context — the frontend renders these as
   *  inline chips under the parent node with click-through links to the
   *  child submission. */
  assignments: AssignedChild[];
}

export interface Submission {
  /** Stable key the submission is addressed by while still a draft. */
  handle: string;
  /** Minted, canonical id — null until its source value is available.
   *  Once set, the submission is resumable at /submissions/{id}. */
  submission_id: string | null;
  form_id: string;
  state: string;
  started_at: string;
  tasks: TaskInstance[];
  /** Pinned form version (human-facing integer). */
  form_version: number;
  /** The form's current live version. When greater than
   *  `form_version`, an admin can re-pin this submission. Surfaced
   *  on the active-fill payload so the edit/reset modal can offer
   *  "use latest form" inline without an extra round-trip. */
  live_form_version: number;
  /** Minor (source-only) version counterparts. Zero when the form
   *  has never had a non-structural revision since the matching
   *  major. The modal compares on the full (major, minor) tuple so
   *  a minor-only difference (a body-only edit) still surfaces the
   *  "use latest" affordance. */
  form_minor_version: number;
  live_minor_version: number;
}

// States we consider "done" — polling stops (or slows down) at these.
export const TERMINAL_SUBMISSION_STATES = new Set(["success", "failed"]);

export function startSubmission(
  formId: string,
  values: Record<string, unknown>,
): Promise<Submission> {
  return request<Submission>(
    `/forms/${encodeURIComponent(formId)}/submissions`,
    {
      method: "POST",
      body: JSON.stringify({ values }),
    },
  );
}

export function getSubmission(
  formId: string,
  submissionId: string,
): Promise<Submission> {
  return request<Submission>(
    `/forms/${encodeURIComponent(formId)}/submissions/${encodeURIComponent(submissionId)}`,
  );
}

// ---- Clear -----------------------------------------------------------------

export interface ClearResponse {
  affected_tasks: string[];
  cleared: boolean;
}

export async function clearSubmission(
  formId: string,
  submissionId: string,
  params: {
    from_task_id?: string;
    dry_run?: boolean;
    mode?: "reset" | "edit";
    /** For an edit: "cascade" runs the dependency-aware cascade on
     *  re-submit; "node_only" leaves downstream steps untouched. */
    scope?: "cascade" | "node_only";
  } = {},
): Promise<ClearResponse> {
  return request<ClearResponse>(
    `/forms/${encodeURIComponent(formId)}/submissions/${encodeURIComponent(submissionId)}/clear`,
    {
      method: "POST",
      body: JSON.stringify({
        from_task_id: params.from_task_id ?? null,
        dry_run: params.dry_run ?? false,
        mode: params.mode ?? "reset",
        scope: params.scope ?? "cascade",
      }),
    },
  );
}

// ---- Retry -----------------------------------------------------------------

export interface RefreshResponse {
  forms: string[];
  load_errors: Record<string, string>;
}

/** Re-scan the workflow source, rebuilding the form registry from the
 *  current workflow files — without restarting the server. */
export async function refreshForms(): Promise<RefreshResponse> {
  return request<RefreshResponse>("/refresh", { method: "POST" });
}

export interface RefreshFormResponse {
  form_id: string;
  status: "live" | "error";
  version_id?: number;
  error?: string;
}

/** Reparse the workflow source and report the result for one form. */
export async function refreshForm(
  formId: string,
): Promise<RefreshFormResponse> {
  return request<RefreshFormResponse>(
    `/forms/${encodeURIComponent(formId)}/refresh`,
    { method: "POST" },
  );
}

// ---- Step ------------------------------------------------------------------

/** A node's submitted response: the field values plus which button
 *  was clicked. */
export interface StepResponse {
  values: Record<string, unknown>;
  button: string | null;
}

export interface StepDetail {
  /** Stable key the submission is addressed by while still a draft. */
  handle: string;
  /** Minted, canonical id — null while the submission is a draft. */
  submission_id: string | null;
  step_id: string;
  /** The layout tree the frontend renders. */
  layout: Block;
  response_received: boolean;
  response: StepResponse | null;
  /** Answers carried over when the step was re-opened — null for a
   *  blank step. The form seeds itself with these. */
  draft: { values: Record<string, unknown>; button: string | null } | null;
  /** Edit-cascade status of this step. */
  status: CascadeStatus;
  /** True when this is the node the user is actively editing — a
   *  re-opened step not yet re-submitted. The Cancel affordance shows
   *  only then. */
  edit_in_progress: boolean;
  responded_at: string | null;
  /** The page this step belongs to — null for a top-level node. */
  page_id: string | null;
  page_title: string | null;
  /** Failure message when a chain step downstream of this node's submit
   *  failed (a backend that raised, an operator that errored).
   *  Null when the chain hasn't failed. Surfaced on the node card. */
  error: string | null;
}

export function getStepDetail(
  formId: string,
  submissionId: string,
  stepId: string,
): Promise<StepDetail> {
  return request<StepDetail>(
    `/forms/${encodeURIComponent(formId)}/submissions/${encodeURIComponent(submissionId)}/steps/${encodeURIComponent(stepId)}`,
  );
}

export function submitStep(
  formId: string,
  submissionId: string,
  stepId: string,
  values: Record<string, unknown>,
  button: string | null,
): Promise<StepDetail> {
  return request<StepDetail>(
    `/forms/${encodeURIComponent(formId)}/submissions/${encodeURIComponent(submissionId)}/steps/${encodeURIComponent(stepId)}`,
    {
      method: "POST",
      body: JSON.stringify({ values, button }),
    },
  );
}

/** Submit a user's response to an Airflow HITL task, resuming the DAG. */
export function respondToHitl(
  formId: string,
  submissionId: string,
  taskId: string,
  chosenOptions: string[],
  paramsInput: Record<string, unknown>,
): Promise<StepDetail> {
  return request<StepDetail>(
    `/forms/${encodeURIComponent(formId)}/submissions/${encodeURIComponent(submissionId)}/hitl/${encodeURIComponent(taskId)}`,
    {
      method: "POST",
      body: JSON.stringify({
        chosen_options: chosenOptions,
        params_input: paramsInput,
      }),
    },
  );
}

// ---- Form metadata --------------------------------------------------------

export interface FormLandingStep {
  step_id: string;
  /** The layout tree rendered on the form's landing page. */
  layout: Block;
}

export interface FormDetail {
  form_id: string;
  title: string;
  description: string;
  landing_step: FormLandingStep;
  /** The form's custom theme, or null when uncustomized. */
  theme: Theme | null;
}

export function getFormDetail(formId: string): Promise<FormDetail> {
  return request<FormDetail>(`/forms/${encodeURIComponent(formId)}`);
}

/** A form's custom theme, or null when it hasn't been customized. */
export interface Comment {
  id: number;
  thread_id: string;
  author: string;
  user_id: string | null;
  body: string;
  created_at: string;
}

/** A component thread's comments, oldest first. */
export function getComments(
  formId: string,
  submissionId: string,
  threadId: string,
): Promise<Comment[]> {
  return request<Comment[]>(
    `/forms/${encodeURIComponent(formId)}/submissions/${encodeURIComponent(
      submissionId,
    )}/comments/${encodeURIComponent(threadId)}`,
  );
}

export function postComment(
  formId: string,
  submissionId: string,
  threadId: string,
  body: string,
): Promise<Comment> {
  return request<Comment>(
    `/forms/${encodeURIComponent(formId)}/submissions/${encodeURIComponent(
      submissionId,
    )}/comments/${encodeURIComponent(threadId)}`,
    { method: "POST", body: JSON.stringify({ body }) },
  );
}

export function getFormTheme(formId: string): Promise<Theme | null> {
  return request<Theme | null>(
    `/forms/${encodeURIComponent(formId)}/theme`,
  );
}

/** Save a form's theme — the full token set. */
export function saveFormTheme(
  formId: string,
  theme: Theme,
): Promise<Theme> {
  return request<Theme>(`/forms/${encodeURIComponent(formId)}/theme`, {
    method: "PUT",
    body: JSON.stringify(theme),
  });
}

// --- Superset dashboards ---------------------------------------------------

/**
 * A refresh requested by a `superset.RefreshDashboard` operator at some
 * point in the chain. Rides the polled submission state, so an open
 * dashboard block picks it up with no extra transport.
 */
export interface DashboardRefresh {
  dashboard: string;
  /** The Superset `time_range` to apply. Minted server-side and
   *  strictly advancing, so each refresh moves the query cache key. */
  time_range: string;
  /** Handled-once marker: a block refreshes per token it has not seen,
   *  so re-polling the same chain state re-triggers nothing. */
  token: string;
}

/** What a dashboard block needs to embed itself. */
export interface DashboardEmbedConfig {
  name: string;
  /** Superset's browser-facing origin — not necessarily how the server
   *  reaches it, which is often an internal hostname. */
  superset_domain: string;
  /** From Superset's embed config; null until provisioning completes. */
  embed_uuid: string | null;
  /** The native time-range filter RefreshDashboard drives. Null means
   *  the dashboard renders but will not update in place. */
  filter_id: string | null;
}

/**
 * Resolve a dashboard name for a form.
 *
 * Both dashboard endpoints are form-scoped: a form grants access to the
 * dashboards it displays and no others, so a guest token cannot be
 * obtained for an arbitrary dashboard by naming it.
 */
export function getDashboardEmbedConfig(
  formId: string,
  name: string,
): Promise<DashboardEmbedConfig> {
  return request<DashboardEmbedConfig>(
    `/forms/${encodeURIComponent(formId)}/dashboards/${encodeURIComponent(name)}/embed`,
  );
}

/**
 * Mint a short-lived Superset guest token.
 *
 * The embedded SDK calls this repeatedly — guest tokens last about five
 * minutes — so it is deliberately cheap on the server.
 */
export async function getDashboardGuestToken(
  formId: string,
  name: string,
): Promise<string> {
  const { token } = await request<{ token: string }>(
    `/forms/${encodeURIComponent(formId)}/dashboards/${encodeURIComponent(name)}/guest-token`,
    { method: "POST" },
  );
  return token;
}

/** Clear a form's custom theme — it reverts to the default. */
export function clearFormTheme(formId: string): Promise<unknown> {
  return request(`/forms/${encodeURIComponent(formId)}/theme`, {
    method: "DELETE",
  });
}

// ---- Connection store -----------------------------------------------------

/** A stored connection as the API returns it — metadata only, never
 *  the credentials. */
export interface Connection {
  name: string;
  conn_type: string;
  base_url: string;
  auth_kind: "basic" | "token" | "aws";
  created_at: string;
  updated_at: string;
}

/** Create/update payload. Credentials are write-only — supply them to
 *  set or rotate, omit them on an update to keep the stored ones. */
export interface ConnectionInput {
  conn_type: string;
  base_url: string;
  auth_kind: "basic" | "token" | "aws";
  username?: string;
  password?: string;
  token?: string;
  // AWS credentials — populated when auth_kind === "aws".
  aws_access_key_id?: string;
  aws_secret_access_key?: string;
  aws_session_token?: string;
  aws_region?: string;
}

/** Every stored connection. */
export function listConnections(): Promise<Connection[]> {
  return request<Connection[]>("/connections");
}

/** Create or update a connection. */
export function saveConnection(
  name: string,
  body: ConnectionInput,
): Promise<Connection> {
  return request<Connection>(`/connections/${encodeURIComponent(name)}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

/** Delete a connection. */
export function deleteConnection(name: string): Promise<unknown> {
  return request(`/connections/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

// ---- Workflow structural graph --------------------------------------------

/** A node-group — a container enclosing its member nodes. */
export interface GraphGroup {
  id: string;
  title: string;
  page_id: string | null;
  /** The node submits via an @backend.branch. */
  is_branch: boolean;
}

/** A graph node — an input, a node-internal backend, an external task,
 *  or a standalone workflow-level backend step. */
export interface GraphNode {
  id: string;
  kind: "input" | "backend" | "airflow" | "workflow_backend";
  /** The node-group this belongs to; null for a workflow-level step. */
  group_id: string | null;
  label: string;
  /** Input type, the Airflow operator kind, or "branch" for a branch
   *  backend. */
  detail: string | null;
  /** In-group rank — inputs 0, internal backend 1, Airflow operators 2+. */
  rank: number;
  is_branch: boolean;
  required: boolean;
}

/** A graph edge — typed by what it represents. */
export interface GraphEdge {
  id: string;
  from_node: string;
  to_node: string;
  /** "in_group" — inputs → backend → tasks; "execution" — the `>>`
   *  flow between top-level steps; "dependency" — a `steps.X.Y` ref. */
  relation: "in_group" | "execution" | "dependency";
  /** For a dependency edge. */
  dep_source: "options" | "default" | "condition" | "argument" | null;
  dep_kind: "functional" | "display" | null;
}

export interface WorkflowGraph {
  form_id: string;
  title: string;
  groups: GraphGroup[];
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export function getFormGraph(formId: string): Promise<WorkflowGraph> {
  return request<WorkflowGraph>(
    `/forms/${encodeURIComponent(formId)}/graph`,
  );
}

/**
 * Raw Python source for a form — the `.py` file the live workflow
 * was compiled from. The Source tab renders this read-only with
 * basic monospace styling. Admin-gated on the backend; non-admins
 * get a 403.
 *
 * `version` is the human-facing integer the source was compiled
 * from (0 means "in-memory only — no persisted version row").
 */
export interface FormSource {
  form_id: string;
  version: number;
  /**
   * Non-structural revision within `version`. Bumps when the source
   * text changes but the compiled graph is identical (e.g. edits to
   * helper functions). Zero for forms that have never had a minor
   * revision. Display as `v{version}.{minor_version}`, suppressing
   * the suffix when 0.
   */
  minor_version: number;
  source: string;
}

export function getFormSource(formId: string): Promise<FormSource> {
  return request<FormSource>(
    `/forms/${encodeURIComponent(formId)}/source`,
  );
}

/**
 * Source pinned to a SPECIFIC form_version — what a given submission
 * was actually running, even after the live form has been edited and
 * the version bumped past it. Used by SubmissionDetailPage's Source
 * tab so an investigator looking at an old submission sees the exact
 * code that ran, not the current live source.
 */
export function getFormVersionSource(
  formId: string,
  version: number,
): Promise<FormSource> {
  return request<FormSource>(
    `/forms/${encodeURIComponent(formId)}/versions/${version}/source`,
  );
}

// ---- Forms index + submission listing -------------------------------------

/** Submission counts for a form, broken out by state. */
export interface SubmissionCounts {
  running: number;
  success: number;
  failed: number;
  total: number;
}

/** One form in the forms index (`GET /forms`). */
export interface FormSummary {
  form_id: string;
  name: string;
  /** Relative folder of the form's file; "" for the top level. */
  folder_path: string;
  /** Whether the form's DSL file is still present in the latest scan. */
  is_live: boolean;
  version_count: number;
  submissions: SubmissionCounts;
  /** ISO timestamp of the form's most recent submission activity, or
   *  null when it has no submissions. */
  last_activity: string | null;
  /** The submission_id whose event produced `last_activity`. Null
   *  when the form has no events yet. Lets the form-summary "Last
   *  activity" KPI link to the source submission. */
  last_activity_submission_id: string | null;
  /** The state of `last_activity_submission_id` — `running`,
   *  `success`, or `failed`. Drives the color of the "Last
   *  activity" timestamp so the kind of activity reads at a
   *  glance. Null when no events yet. */
  last_activity_state: string | null;
}

/** One row in a form's submission list. */
export interface SubmissionSummary {
  submission_id: string | null;
  handle: string;
  state: string;
  /** The form version (human-facing integer) this submission ran on. */
  form_version: number;
  created_at: string;
  /** Last activity timestamp — bumped on every state change.
   *  Drives the "Last activity" column and the `updated_at` sort. */
  updated_at: string | null;
  terminated_at: string | null;
  /** Tombstone marker. Populated only when the row is a soft-
   *  deleted submission AND the listing was called with
   *  `show_deleted=1` AND the caller is admin. Drives the
   *  "deleted" pill + the non-clickable row treatment. */
  deleted_at: string | null;
  /** Node id of the submission's current (latest) step. */
  current_step: string | null;
}

/** Sort direction on a sortable listing column. */
export type SortDirection = "asc" | "desc";

/** One entry in a multi-column sort spec — first entry is the
 *  primary sort, subsequent ones break ties. The string form on
 *  the wire is `"column:direction"`; this is the in-memory shape
 *  the UI binds to header click handlers. */
export interface SortEntry {
  column: string;
  direction: SortDirection;
}

/** Page envelope from `GET /forms/{id}/submissions`. `total` is the
 *  filtered count BEFORE limit/offset, so the UI can render a
 *  "Showing M–N of T" footer and gate the Next button. */
export interface SubmissionListingPage {
  submissions: SubmissionSummary[];
  total: number;
  limit: number;
  offset: number;
}

/** Filters + sort + pagination state the UI passes through to the
 *  paginated listing endpoint. All fields optional; unset fields
 *  fall through to server defaults (limit=25, offset=0, no
 *  state/q filter, sort by created_at:desc). */
export interface SubmissionListingQuery {
  limit?: number;
  offset?: number;
  states?: string[];
  q?: string;
  sort?: SortEntry[];
  /** Calendar date (`YYYY-MM-DD`) or ISO datetime — backend parses
   *  either. Date inputs in the UI emit calendar dates. */
  updatedSince?: string;
  updatedBefore?: string;
  /** Multi-select on the submission's current step node_id. */
  currentSteps?: string[];
  /** Admin-only — when true, soft-deleted rows are included in
   *  the listing (the backend ignores this flag for non-admins). */
  showDeleted?: boolean;
}

/** One row in the current-step filter dropdown — node_id plus the
 *  count of submissions currently at that step. */
export interface CurrentStepOption {
  node_id: string;
  count: number;
}

export function listForms(): Promise<FormSummary[]> {
  return request<FormSummary[]>("/forms");
}

/** Fetch one page of a form's submission list with filter + sort.
 *  The repeated-param encoding matches the FastAPI endpoint:
 *  multi-select `state` / `current_step` and multi-column `sort`
 *  each show up as multiple query params with the same name. */
export function getFormSubmissions(
  formId: string,
  query: SubmissionListingQuery = {},
): Promise<SubmissionListingPage> {
  const params = new URLSearchParams();
  if (query.limit !== undefined) params.set("limit", String(query.limit));
  if (query.offset !== undefined) params.set("offset", String(query.offset));
  for (const s of query.states ?? []) params.append("state", s);
  if (query.q) params.set("q", query.q);
  for (const s of query.sort ?? []) {
    params.append("sort", `${s.column}:${s.direction}`);
  }
  if (query.updatedSince) {
    params.set("updated_since", query.updatedSince);
  }
  if (query.updatedBefore) {
    params.set("updated_before", query.updatedBefore);
  }
  for (const step of query.currentSteps ?? []) {
    params.append("current_step", step);
  }
  if (query.showDeleted) params.set("show_deleted", "1");
  const qs = params.toString();
  return request<SubmissionListingPage>(
    `/forms/${encodeURIComponent(formId)}/submissions${qs ? `?${qs}` : ""}`,
  );
}

/** Fetch the distinct `current_step` values across a form's
 *  submissions (with per-step counts), for the listing's current-
 *  step filter dropdown. Admin + `showDeleted` includes tombstoned
 *  rows in the counts (silently ignored otherwise). */
export function getFormSubmissionCurrentSteps(
  formId: string,
  options: { showDeleted?: boolean } = {},
): Promise<CurrentStepOption[]> {
  const params = new URLSearchParams();
  if (options.showDeleted) params.set("show_deleted", "1");
  const qs = params.toString();
  return request<CurrentStepOption[]>(
    `/forms/${encodeURIComponent(formId)}/submissions/current-steps${
      qs ? `?${qs}` : ""
    }`,
  );
}

/** Response from `POST /forms/{id}/submissions/delete` — the soft-
 *  delete endpoint. `deleted` is the subset of input handles that
 *  were actually tombstoned (DB row got `deleted_at` stamped); the
 *  rest landed in `not_found`. Both are echoed so the UI can render
 *  an accurate toast even on partial failures (stale listings, an
 *  admin in another tab having already deleted some). */
export interface SoftDeleteSubmissionsResponse {
  deleted: string[];
  not_found: string[];
}

/** Soft-delete a batch of submissions for a form. Admin-only on the
 *  server; non-admins get 403 (and the UI hides the checkbox column
 *  for them so they shouldn't reach this call). */
export function deleteSubmissions(
  formId: string, handles: string[],
): Promise<SoftDeleteSubmissionsResponse> {
  return request<SoftDeleteSubmissionsResponse>(
    `/forms/${encodeURIComponent(formId)}/submissions/delete`,
    { method: "POST", body: JSON.stringify({ handles }) },
  );
}

// --- Analytics --------------------------------------------------------------
//
// One endpoint per visualization on the Reports tab. The frontend
// drives URL-param filters and the server applies them; both sides
// agree on a small filter shape (state list, current_step list, date
// range preset or explicit start/end). Each chart's data is fetched
// independently, so toggling a state filter only re-fetches the
// charts that consume state-filtered data.

/** A single bar in a categorical analytics chart. */
export interface AnalyticsBucket {
  /** Stable id the frontend uses for filter URLs (state name, node id). */
  key: string;
  /** Human-readable label for display. */
  label: string;
  /** Bar height. */
  count: number;
}

/** Response shape for the bar-chart analytics endpoints. The
 *  `filters_applied` block echoes what the server actually filtered
 *  on, including resolved date ranges and the preset name (if any). */
export interface AnalyticsResponse {
  buckets: AnalyticsBucket[];
  total: number;
  filters_applied: {
    state: string[] | null;
    current_step: string[] | null;
    start_date: string | null;
    end_date: string | null;
    date_range_preset: string | null;
  };
}

/** Filters sent on every analytics request. All optional; absent
 *  fields fall back to the form's `@form(reports=...)` defaults
 *  (which themselves fall back to framework defaults — last 30 days,
 *  no state/current_step filter). */
export interface AnalyticsFilters {
  state?: string[];
  current_step?: string[];
  date_range?:
    | "all_time"
    | "last_7_days"
    | "last_30_days"
    | "last_90_days";
  start_date?: string;
  end_date?: string;
}

function _analyticsQuery(filters: AnalyticsFilters): string {
  const p = new URLSearchParams();
  if (filters.state) {
    for (const s of filters.state) p.append("state", s);
  }
  if (filters.current_step) {
    for (const s of filters.current_step) p.append("current_step", s);
  }
  if (filters.date_range) p.set("date_range", filters.date_range);
  if (filters.start_date) p.set("start_date", filters.start_date);
  if (filters.end_date) p.set("end_date", filters.end_date);
  const s = p.toString();
  return s ? `?${s}` : "";
}

/** The form's resolved default analytics filters — framework
 *  defaults merged with `@form(reports=...)` overrides. The Reports
 *  tab fetches this on mount and seeds URL query params so the URL
 *  is always the source of truth for active filters. */
export interface AnalyticsDefaults {
  state: string[] | null;
  current_step: string[] | null;
  date_range: string | null;
}

export function getAnalyticsDefaults(
  formId: string,
): Promise<AnalyticsDefaults> {
  return request<AnalyticsDefaults>(
    `/forms/${encodeURIComponent(formId)}/analytics/defaults`,
  );
}

export function getAnalyticsState(
  formId: string,
  filters: AnalyticsFilters = {},
): Promise<AnalyticsResponse> {
  return request<AnalyticsResponse>(
    `/forms/${encodeURIComponent(formId)}/analytics/state${_analyticsQuery(filters)}`,
  );
}

export function getAnalyticsCurrentStep(
  formId: string,
  filters: AnalyticsFilters = {},
): Promise<AnalyticsResponse> {
  return request<AnalyticsResponse>(
    `/forms/${encodeURIComponent(formId)}/analytics/current_step${_analyticsQuery(filters)}`,
  );
}

export function getAnalyticsStepCounts(
  formId: string,
  filters: AnalyticsFilters = {},
): Promise<AnalyticsResponse> {
  return request<AnalyticsResponse>(
    `/forms/${encodeURIComponent(formId)}/analytics/step_counts${_analyticsQuery(filters)}`,
  );
}

/** A node in the submission-flow sankey. `reach` is the count of
 *  submissions that ever reached this node. `terminals` is the
 *  per-fate breakdown of submissions whose path *ended* here —
 *  `failed` (the chain errored at this node), `in_flight`
 *  (currently parked here), and `succeeded` (reached a terminal
 *  state at this node). Keys are absent when their counts are
 *  zero so the renderer doesn't draw empty sub-stacks. */
export interface FlowNode {
  node_id: string;
  label: string;
  reach: number;
  terminals: { failed?: number; in_flight?: number; succeeded?: number };
}

/** One form-graph edge with the count of submissions that took it. */
export interface FlowEdge {
  source: string;
  target: string;
  count: number;
}

export interface FlowResponse {
  nodes: FlowNode[];
  edges: FlowEdge[];
  total: number;
  filters_applied: AnalyticsResponse["filters_applied"];
}

export function getAnalyticsFlow(
  formId: string,
  filters: AnalyticsFilters = {},
): Promise<FlowResponse> {
  return request<FlowResponse>(
    `/forms/${encodeURIComponent(formId)}/analytics/flow${_analyticsQuery(filters)}`,
  );
}

// --- Time-shaped analytics --------------------------------------------------
//
// completion_time + step_time + step_time/{node_id} + throughput.
// All accept the same filter shape as the other analytics endpoints;
// the framework defaults to terminal states (`success` + `failed`)
// so time charts focus on completed work without the author having
// to opt in.

/** A single bucket on a duration histogram. The interval is
 *  `[lo_seconds, hi_seconds)`; the `label` is pre-formatted for
 *  the axis (e.g. "30s–45s"). */
export interface HistogramBucket {
  lo_seconds: number;
  hi_seconds: number;
  label: string;
  count: number;
}

export interface CompletionTimeResponse {
  buckets: HistogramBucket[];
  total: number;
  mean_seconds: number | null;
  p50_seconds: number | null;
  p90_seconds: number | null;
  /** Submissions matching the page filters (the population the
   *  histogram is computed over). */
  matching_submissions: number;
  /** Subset of `matching_submissions` that have a `terminated_at`
   *  timestamp — i.e., have actually completed. The histogram only
   *  uses these. When `matching_submissions > 0` but
   *  `with_terminated_at === 0`, the page can render a more
   *  informative empty state. */
  with_terminated_at: number;
  filters_applied: AnalyticsResponse["filters_applied"];
}

export function getAnalyticsCompletionTime(
  formId: string,
  filters: AnalyticsFilters = {},
): Promise<CompletionTimeResponse> {
  return request<CompletionTimeResponse>(
    `/forms/${encodeURIComponent(formId)}/analytics/completion_time${_analyticsQuery(filters)}`,
  );
}

/** Per-step time-in-step aggregate. `count` is the number of
 *  *completed* visits (both started_at and submitted_at present);
 *  the percentiles cover that same population. */
export interface StepTimeBucket {
  node_id: string;
  label: string;
  count: number;
  mean_seconds: number;
  p10_seconds: number;
  p50_seconds: number;
  p90_seconds: number;
}

export interface StepTimeResponse {
  steps: StepTimeBucket[];
  filters_applied: AnalyticsResponse["filters_applied"];
}

export function getAnalyticsStepTime(
  formId: string,
  filters: AnalyticsFilters = {},
): Promise<StepTimeResponse> {
  return request<StepTimeResponse>(
    `/forms/${encodeURIComponent(formId)}/analytics/step_time${_analyticsQuery(filters)}`,
  );
}

/** Drill-down histogram for one step's visit-duration distribution.
 *  Driven by a bar click in the time-per-step chart. */
export interface StepHistogramResponse {
  node_id: string;
  label: string;
  buckets: HistogramBucket[];
  total: number;
  mean_seconds: number | null;
  p50_seconds: number | null;
  p90_seconds: number | null;
  filters_applied: AnalyticsResponse["filters_applied"];
}

export function getAnalyticsStepHistogram(
  formId: string,
  nodeId: string,
  filters: AnalyticsFilters = {},
): Promise<StepHistogramResponse> {
  return request<StepHistogramResponse>(
    `/forms/${encodeURIComponent(formId)}/analytics/step_time/${encodeURIComponent(nodeId)}${_analyticsQuery(filters)}`,
  );
}

/** One time bucket on the throughput chart. `start` is the bucket's
 *  start datetime (ISO); `counts` is state → count of submissions
 *  *started* in this bucket, colored by their state at query time. */
export interface ThroughputBucket {
  start: string;
  counts: Record<string, number>;
}

export type ThroughputInterval = "day" | "week" | "month";

export interface ThroughputResponse {
  buckets: ThroughputBucket[];
  interval: ThroughputInterval;
  total: number;
  filters_applied: AnalyticsResponse["filters_applied"];
}

export function getAnalyticsThroughput(
  formId: string,
  filters: AnalyticsFilters = {},
  interval?: ThroughputInterval,
): Promise<ThroughputResponse> {
  const q = _analyticsQuery(filters);
  const sep = q ? "&" : "?";
  const intervalParam = interval ? `${sep}interval=${interval}` : "";
  return request<ThroughputResponse>(
    `/forms/${encodeURIComponent(formId)}/analytics/throughput${q}${intervalParam}`,
  );
}

/** Fine-grained submission-rate intervals. Wider range than the
 *  throughput chart since this one's job is to surface short
 *  spikes (attacks, viral inbound) as well as long-range baselines. */
export type SubmissionRateInterval =
  | "minute"
  | "5min"
  | "15min"
  | "hour"
  | "day"
  | "week"
  | "month";

/** One time bucket on the submission-rate line. No state breakdown
 *  by design — the question is "did volume spike?" and color stacks
 *  bury that signal. */
export interface SubmissionRateBucket {
  start: string;
  count: number;
}

export interface SubmissionRateResponse {
  buckets: SubmissionRateBucket[];
  interval: SubmissionRateInterval;
  total: number;
  peak_count: number;
  peak_start: string | null;
  mean_count: number;
  filters_applied: AnalyticsResponse["filters_applied"];
}

export function getAnalyticsSubmissionRate(
  formId: string,
  filters: AnalyticsFilters = {},
  interval?: SubmissionRateInterval,
): Promise<SubmissionRateResponse> {
  const q = _analyticsQuery(filters);
  const sep = q ? "&" : "?";
  const intervalParam = interval ? `${sep}interval=${interval}` : "";
  return request<SubmissionRateResponse>(
    `/forms/${encodeURIComponent(formId)}/analytics/submission_rate${q}${intervalParam}`,
  );
}

/** One step in a submission's persisted record. */
export interface StepDetailRow {
  seq: number;
  node_id: string;
  /** Node display title — null for a workflow-level backend step. */
  title: string | null;
  page_id: string | null;
  kind: string; // "node" | "backend"
  state: string; // "awaiting" | "submitted" | "failed"
  started_at: string | null;
  submitted_at: string | null;
  form_values: Record<string, unknown> | null;
  /** Legacy: the first backend's return only, kept for back-compat. */
  backend_return: unknown;
  /** Every chain-step's return value, keyed by producer name. The
   *  submission-detail UI prefers this over `backend_return` so
   *  multi-backend nodes display all their outputs. */
  chain_outputs: Record<string, unknown> | null;
  button_clicked: string | null;
  /** Display labels for picker-field values (Phase 4) — replaces bare
   *  identifiers like `user_id=1` with the resolved label like "admin"
   *  in the submission summary. Shape: `{field_name: {identifier: label}}`.
   *  The frontend renders `label (identifier)` so the underlying value
   *  is still visible. Null when no picker fields are in this step. */
  value_labels: Record<string, Record<string, string>> | null;
  /** Identifier kind per picker field (so the frontend can link
   *  user-id values to the /users page). Shape:
   *  `{field_name: "frontflow_user_id" | "external_id" | ...}`. Null
   *  when no picker fields are in this step. */
  value_kinds: Record<string, string> | null;
  /** Children spawned from this step by an Assign operator. Empty for
   *  steps that didn't fire an Assign. Drives the parent submission's
   *  graph + step-block rendering of inline child chips. */
  assignments: AssignedChild[];
}

/** One entry in a submission's append-only event log. */
export interface EventRow {
  type: string;
  node_id: string | null;
  page_id: string | null;
  occurred_at: string;
  payload: Record<string, unknown> | null;
}

/** A submission's full persisted record (`.../detail`) — every step
 *  with the data it captured, plus the event history. */
export interface VersionOption {
  id: number;
  version: number;
  minor_version: number;
  is_active: boolean;
}

export interface SubmissionDetail {
  submission_id: string | null;
  handle: string;
  form_id: string;
  state: string;
  form_version: number;
  /** The form's current (live) version. When greater than
   *  `form_version`, the submission lags the live form and an admin
   *  can re-pin it via POST /repin. */
  live_form_version: number;
  /** Minor (source-only) version counterparts. Zero when the form
   *  has never had a non-structural revision since the matching
   *  major. Together they form a full `(major, minor)` tuple
   *  compared lexicographically to decide which is newer. */
  form_minor_version: number;
  live_minor_version: number;
  /** The version being viewed — equals `form_version` for the active
   *  chain, or the requested historical version when `?version=<id>`
   *  was passed. Drives the "viewing read-only history" banner. */
  viewing_version: number;
  viewing_version_id: number;
  viewing_minor_version: number;
  is_viewing_active: boolean;
  /** Every form_version this submission has data on, oldest first.
   *  Drives the version picker on the summary page. */
  available_versions: VersionOption[];
  created_at: string;
  terminated_at: string | null;
  error: string | null;
  steps: StepDetailRow[];
  events: EventRow[];
  /** Spawned child submissions to render as nested clusters in the
   *  parent's graph view. Walked BFS by depth from this submission
   *  via the granted_by_submission_handle chain, capped at depth 10,
   *  cycle-guarded. Empty when this submission spawned no children. */
  child_graphs: ChildGraph[];
  /** When the submission's pinned form source no longer compiles,
   *  the backend falls back to deserializing the stored compiled
   *  graph for viewing. This field carries the original compile
   *  error so the UI can show a banner and disable submit/advance
   *  actions. Null when source compiled normally. */
  form_version_compile_error: string | null;
}

export function getSubmissionDetail(
  formId: string,
  submissionId: string,
  versionId?: number,
): Promise<SubmissionDetail> {
  const qs = versionId !== undefined ? `?version=${versionId}` : "";
  return request<SubmissionDetail>(
    `/forms/${encodeURIComponent(formId)}/submissions/${encodeURIComponent(submissionId)}/detail${qs}`,
  );
}

/** Re-pin a submission to the current (live) form version.
 *
 *  On compatible diffs the backend returns 200 with `repinned: true`
 *  and updates the submission's pin. On incompatibility (deleted node,
 *  missing field, type change, etc.) it returns 409 with the issues
 *  list — the caller renders the diff and the submission stays put. */
export interface RepinIssue {
  kind:
    | "node_missing"
    | "field_missing"
    | "field_type_changed"
    | "option_removed"
    | "button_missing";
  node_id: string;
  field?: string | null;
  button?: string | null;
  detail: string;
}

export interface RepinResponse {
  repinned: boolean;
  from_version: number;
  to_version: number;
  issues: RepinIssue[];
  /** On a force=true re-pin, the node ids that survived on the
   *  active chain. Empty when the repin didn't truncate (clean
   *  repin) or wasn't a force-repin. */
  kept_steps: string[];
  /** On a force=true re-pin, the node ids that were dropped from
   *  the active chain (and frozen under the prior form_version_id
   *  as read-only history). Empty when nothing was truncated. */
  dropped_steps: string[];
}

export async function repinSubmission(
  formId: string,
  submissionId: string,
  options: { force?: boolean } = {},
): Promise<RepinResponse> {
  // 200 and 409 both return a RepinResponse body — `repinned` and
  // `issues` distinguish them. Bypass the throwing `request` helper
  // and call fetch directly so we can return the body on 409.
  const params: string[] = [];
  if (options.force) params.push("force=true");
  if (_unlistedKey) params.push(`key=${encodeURIComponent(_unlistedKey)}`);
  const query = params.length ? `?${params.join("&")}` : "";
  const url =
    `${BASE_URL}/forms/${encodeURIComponent(formId)}` +
    `/submissions/${encodeURIComponent(submissionId)}/repin${query}`;
  const res = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
  });
  if (res.status === 200 || res.status === 409) {
    return (await res.json()) as RepinResponse;
  }
  // Any other status — auth, server error — throws as usual.
  let detail = res.statusText;
  try {
    const body = await res.json();
    detail = body.detail ?? detail;
  } catch {}
  throw new ApiError(detail, res.status);
}

// ---- Authentication --------------------------------------------------------

export interface UserInfo {
  username: string;
  is_admin: boolean;
  must_change_password: boolean;
}

/** Authenticate and open a session. The backend sets an HttpOnly
 *  session cookie; on bad credentials this throws ApiError (401). */
export async function login(
  username: string,
  password: string,
): Promise<UserInfo> {
  return request<UserInfo>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

/** End the current session. */
export async function logout(): Promise<void> {
  await request<{ ok: boolean }>("/auth/logout", { method: "POST" });
}

/** The currently authenticated user, or null if not signed in. */
export async function fetchCurrentUser(): Promise<UserInfo | null> {
  try {
    return await request<UserInfo>("/auth/me");
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      return null;
    }
    throw err;
  }
}

// ---- Access management (admin) ---------------------------------------------

export interface GroupSummary {
  id: number;
  name: string;
  member_count: number;
  grant_count: number;
}

export interface FolderGrant {
  id: number;
  folder_path: string;
  role: "view" | "manage";
}

export interface GroupDetail {
  id: number;
  name: string;
  members: { id: number; username: string }[];
  grants: FolderGrant[];
}

export interface AccountInfo {
  id: number;
  username: string;
  is_admin: boolean;
  is_active: boolean;
  must_change_password: boolean;
}

export function listUsers(): Promise<AccountInfo[]> {
  return request<AccountInfo[]>("/users");
}

export function listGroups(): Promise<GroupSummary[]> {
  return request<GroupSummary[]>("/groups");
}

export function createGroup(name: string): Promise<{ id: number }> {
  return request<{ id: number }>("/groups", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}

export function getGroup(groupId: number): Promise<GroupDetail> {
  return request<GroupDetail>(`/groups/${groupId}`);
}

export async function deleteGroup(groupId: number): Promise<void> {
  await request(`/groups/${groupId}`, { method: "DELETE" });
}

export async function addGroupMember(
  groupId: number,
  userId: number,
): Promise<void> {
  await request(`/groups/${groupId}/members`, {
    method: "POST",
    body: JSON.stringify({ user_id: userId }),
  });
}

export async function removeGroupMember(
  groupId: number,
  userId: number,
): Promise<void> {
  await request(`/groups/${groupId}/members/${userId}`, {
    method: "DELETE",
  });
}

export function addGroupGrant(
  groupId: number,
  folderPath: string,
  role: "view" | "manage",
): Promise<FolderGrant> {
  return request<FolderGrant>(`/groups/${groupId}/grants`, {
    method: "POST",
    body: JSON.stringify({ folder_path: folderPath, role }),
  });
}

export async function removeGrant(grantId: number): Promise<void> {
  await request(`/grants/${grantId}`, { method: "DELETE" });
}

// ---- Form visibility (M3) --------------------------------------------------

export type Visibility = "public" | "unlisted" | "restricted";

export interface FormVisibility {
  visibility: Visibility;
  unlisted_token: string | null;
  acl: { id: number; username: string }[];
}

export function getFormVisibility(
  formId: string,
): Promise<FormVisibility> {
  return request<FormVisibility>(
    `/forms/${encodeURIComponent(formId)}/visibility`,
  );
}

export function setFormVisibility(
  formId: string,
  visibility: Visibility,
): Promise<FormVisibility> {
  return request<FormVisibility>(
    `/forms/${encodeURIComponent(formId)}/visibility`,
    { method: "PUT", body: JSON.stringify({ visibility }) },
  );
}

export function regenerateUnlistedToken(
  formId: string,
): Promise<{ unlisted_token: string }> {
  return request<{ unlisted_token: string }>(
    `/forms/${encodeURIComponent(formId)}/unlisted-token/regenerate`,
    { method: "POST" },
  );
}

export async function addFormAclUser(
  formId: string,
  userId: number,
): Promise<void> {
  await request(`/forms/${encodeURIComponent(formId)}/acl`, {
    method: "POST",
    body: JSON.stringify({ user_id: userId }),
  });
}

export async function removeFormAclUser(
  formId: string,
  userId: number,
): Promise<void> {
  await request(
    `/forms/${encodeURIComponent(formId)}/acl/${userId}`,
    { method: "DELETE" },
  );
}

// ---- User management (admin) -----------------------------------------------

export function createUser(
  username: string,
  password: string,
  isAdmin: boolean,
): Promise<{ id: number; username: string; is_admin: boolean }> {
  return request("/users", {
    method: "POST",
    body: JSON.stringify({
      username,
      password,
      is_admin: isAdmin,
    }),
  });
}

export async function resetUserPassword(
  userId: number,
  newPassword: string,
): Promise<void> {
  await request(`/users/${userId}/password`, {
    method: "PUT",
    body: JSON.stringify({ new_password: newPassword }),
  });
}

export async function setUserAdmin(
  userId: number,
  isAdmin: boolean,
): Promise<void> {
  await request(`/users/${userId}/admin`, {
    method: "PUT",
    body: JSON.stringify({ is_admin: isAdmin }),
  });
}

export async function setUserActive(
  userId: number,
  isActive: boolean,
): Promise<void> {
  await request(`/users/${userId}/active`, {
    method: "PUT",
    body: JSON.stringify({ is_active: isActive }),
  });
}

export async function deleteUser(userId: number): Promise<void> {
  await request(`/users/${userId}`, { method: "DELETE" });
}

/** A signed-in user changing their own password. */
export async function changeOwnPassword(
  currentPassword: string,
  newPassword: string,
): Promise<void> {
  await request("/auth/change-password", {
    method: "POST",
    body: JSON.stringify({
      current_password: currentPassword,
      new_password: newPassword,
    }),
  });
}

// ---- File uploads ----------------------------------------------------------

/** The reference returned for an uploaded file — becomes the form
 *  field's value. A transient `file` carries a token; an `s3file`
 *  carries the S3 bucket and key. */
export interface UploadResult {
  kind: "file" | "s3file";
  filename: string;
  size: number;
  content_type: string;
  token?: string | null;
  bucket?: string | null;
  key?: string | null;
}

/** Upload a file for a File / S3File field. Multipart — not JSON — so
 *  this bypasses the JSON request helper. The unlisted key, if any,
 *  is appended so uploads to an unlisted form authorize.
 *
 *  `submissionId` and `draftValues` let an S3File `key` template
 *  resolve: the former against the submission's earlier-step data,
 *  the latter against the upload's own screen. Both are optional —
 *  on the landing screen there is no submission yet. */
export async function uploadFile(
  formId: string,
  fieldId: string,
  file: File,
  opts?: {
    submissionId?: string | null;
    draftValues?: Record<string, unknown>;
  },
): Promise<UploadResult> {
  const body = new FormData();
  body.append("field_id", fieldId);
  body.append("file", file);
  if (opts?.submissionId) {
    body.append("submission_id", opts.submissionId);
  }
  if (opts?.draftValues) {
    body.append("draft_values", JSON.stringify(opts.draftValues));
  }

  let url = `${BASE_URL}/forms/${encodeURIComponent(formId)}/uploads`;
  const key = getUnlistedKey();
  if (key) url += `?key=${encodeURIComponent(key)}`;

  const res = await fetch(url, {
    method: "POST",
    credentials: "include",
    body, // no Content-Type — the browser sets the multipart boundary
  });
  if (!res.ok) {
    let detail = `Upload failed (${res.status})`;
    try {
      const j = await res.json();
      if (j.detail) detail = j.detail as string;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detail, res.status);
  }
  return (await res.json()) as UploadResult;
}

// ---- Assignments + my tasks (Phase 6 / 7) ---------------------------------

/** One spawned child submission shown inline on the parent's chain —
 *  the parent's graph view renders these as a clickable chip under
 *  the node that spawned them.
 *
 *  Visible because the parent's Assign operator fired and granted
 *  `assignee_username` the role `role_id` on the child submission
 *  identified by `child_submission_handle` (or `child_submission_id`
 *  once minted). `revoked_at` is non-null if the grant has since been
 *  revoked; the frontend can still link to the child for audit but
 *  should style differently. */
export interface AssignedChild {
  assignment_id: number;
  child_form_id: string;
  child_form_title: string;
  child_submission_handle: string;
  child_submission_id: string | null;
  child_submission_state: string;
  role_id: string;
  assignee_user_id: number;
  assignee_username: string | null;
  granted_at: string;
  revoked_at: string | null;
}

/** One spawned child submission's graph + state, attached to the
 *  parent submission's response for the nested-graph viz.
 *
 *  The parent's /detail endpoint walks every assignment granted by
 *  this submission (and recursively, by its children) and produces
 *  one ChildGraph per distinct child submission encountered. */
export interface ChildGraph {
  /** Which parent submission + node spawned us. */
  parent_submission_handle: string;
  parent_node_id: string;
  assignment_id: number;
  child_form_id: string;
  child_form_title: string;
  child_submission_handle: string;
  child_submission_id: string | null;
  child_submission_state: string;
  role_id: string;
  assignee_user_id: number;
  assignee_username: string | null;
  granted_at: string;
  revoked_at: string | null;
  /** The child form's static graph — same structure as the
   *  /forms/{id}/graph endpoint. The frontend reuses its existing
   *  graph renderer with this payload. */
  graph: WorkflowGraph;
  /** Per-node run state for this specific child submission. Keys are
   *  node ids in `graph`; values are "succeeded" / "running" /
   *  "failed" / "not_reached". */
  node_state: Record<string, string>;
  /** Depth in the spawn tree — 1 = direct child of the rendered
   *  submission, 2 = grandchild, etc. Capped at 10 to prevent
   *  runaway recursion. */
  depth: number;
}

/** One row in the per-user assignment listing — every active or
 *  historically-revoked SubmissionAssignment for this user. Drives
 *  the admin user-detail page's Access tab. */
export interface UserAssignment {
  assignment_id: number;
  submission_handle: string;
  submission_id: string | null;
  submission_state: string;
  form_id: string;
  form_title: string;
  role_id: string;
  granted_at: string;
  granted_by_user_id: number;
  granted_by_username: string | null;
  revoked_at: string | null;
  revoked_by_user_id: number | null;
  revoked_by_username: string | null;
}

export function listUserAssignments(
  userId: number,
  includeRevoked: boolean = true,
): Promise<UserAssignment[]> {
  const qs = includeRevoked ? "" : "?include_revoked=false";
  return request<UserAssignment[]>(`/users/${userId}/assignments${qs}`);
}

export function revokeAssignment(assignmentId: number): Promise<void> {
  return request<void>(`/assignments/${assignmentId}/revoke`, {
    method: "POST",
  });
}

/** Revoke every active assignment a user holds on one submission.
 *  Returns the count of rows actually flipped (zero is a valid
 *  response — the endpoint is idempotent). */
export function revokeAllForUserOnSubmission(
  submissionHandle: string,
  userId: number,
): Promise<{ revoked_count: number }> {
  const path =
    `/submissions/${encodeURIComponent(submissionHandle)}` +
    `/users/${userId}/revoke-all`;
  return request<{ revoked_count: number }>(path, { method: "POST" });
}

/** One row in the signed-in user's /my-tasks inbox — every active
 *  assignment granted to them, newest first. */
export interface MyTask {
  assignment_id: number;
  submission_handle: string;
  submission_id: string | null;
  submission_state: string;
  form_id: string;
  form_title: string;
  role_id: string;
  granted_at: string;
}

export function listMyTasks(): Promise<MyTask[]> {
  return request<MyTask[]>("/my-tasks");
}

// --- Form-version diff -----------------------------------------------------

/** One line of a unified diff returned by /forms/.../diff/. `kind`
 *  drives the row's background color (green/red/neutral); `text` is
 *  the raw content without the +/-/space marker. Line numbers are
 *  null on the side a line doesn't exist on. */
export interface DiffLine {
  kind: "context" | "add" | "remove";
  text: string;
  from_lineno: number | null;
  to_lineno: number | null;
}

/** One contiguous hunk in the diff. The `header` mirrors git's
 *  `@@ -a,b +c,d @@` line; the numeric fields are pre-parsed so the
 *  UI can avoid re-parsing. */
export interface DiffHunk {
  header: string;
  from_start: number;
  from_count: number;
  to_start: number;
  to_count: number;
  lines: DiffLine[];
}

export interface FormVersionDiffSide {
  form_version_id: number;
  version: number;
  minor_version: number;
}

export interface FormVersionDiffResponse {
  from_version: FormVersionDiffSide;
  to_version: FormVersionDiffSide;
  bump: "none" | "minor" | "major";
  added_lines: number;
  removed_lines: number;
  hunks: DiffHunk[];
}

export function diffFormVersions(
  formId: string, fromId: number, toId: number,
): Promise<FormVersionDiffResponse> {
  return request<FormVersionDiffResponse>(
    `/forms/${encodeURIComponent(formId)}/versions/${fromId}/diff/${toId}`,
  );
}
