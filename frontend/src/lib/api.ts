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
  kind: "hitl" | "external" | "backend";
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
  /** Whether this step may be rerun on its own from the UI. When
   *  false, the per-step rerun menu is suppressed. */
  retryable: boolean;
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
  /** True for the workflow's landing step. When true the per-step
   *  reset affordance is suppressed — the landing step sets the
   *  submission id and can only be rewound via a full reset. */
  is_landing: boolean;
  /** The page this step belongs to — null for a top-level node. */
  page_id: string | null;
  page_title: string | null;
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
  auth_kind: "basic" | "token";
  created_at: string;
  updated_at: string;
}

/** Create/update payload. Credentials are write-only — supply them to
 *  set or rotate, omit them on an update to keep the stored ones. */
export interface ConnectionInput {
  conn_type: string;
  base_url: string;
  auth_kind: "basic" | "token";
  username?: string;
  password?: string;
  token?: string;
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
  is_landing: boolean;
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
}

/** One row in a form's submission list (`GET /forms/{id}/submissions`). */
export interface SubmissionSummary {
  submission_id: string | null;
  handle: string;
  state: string;
  /** The form version (human-facing integer) this submission ran on. */
  form_version: number;
  created_at: string;
  terminated_at: string | null;
  /** Node id of the submission's current (latest) step. */
  current_step: string | null;
}

export function listForms(): Promise<FormSummary[]> {
  return request<FormSummary[]>("/forms");
}

export function getFormSubmissions(
  formId: string,
): Promise<SubmissionSummary[]> {
  return request<SubmissionSummary[]>(
    `/forms/${encodeURIComponent(formId)}/submissions`,
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
  backend_return: unknown;
  button_clicked: string | null;
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
export interface SubmissionDetail {
  submission_id: string | null;
  handle: string;
  form_id: string;
  state: string;
  form_version: number;
  created_at: string;
  terminated_at: string | null;
  error: string | null;
  steps: StepDetailRow[];
  events: EventRow[];
}

export function getSubmissionDetail(
  formId: string,
  submissionId: string,
): Promise<SubmissionDetail> {
  return request<SubmissionDetail>(
    `/forms/${encodeURIComponent(formId)}/submissions/${encodeURIComponent(submissionId)}/detail`,
  );
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
