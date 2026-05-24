import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  deleteConnection,
  saveConnection,
  type Connection,
  type ConnectionInput,
} from "../lib/api";
import { formatTimestamp } from "../lib/format";
import {
  CONNECTION_TYPES,
  connectionType,
  type AuthKind,
} from "../lib/connectionTypes";
import { useConnections } from "../hooks/useConnections";

/**
 * The connection store (`/connections`) — every credentialed endpoint
 * workflow operators authenticate against. Currently Airflow instances.
 *
 * Credentials are encrypted at rest on the backend and never returned
 * to the client, so the editor shows empty credential fields: filling
 * them rotates the secret, leaving them blank keeps the stored one.
 */

/** Editor state — closed, adding a new connection, or editing one. */
type EditorTarget = null | "new" | Connection;

export default function ConnectionsPage() {
  const { data: connections, error, isLoading } = useConnections();
  const [editing, setEditing] = useState<EditorTarget>(null);

  return (
    <main className="relative z-10 mx-auto max-w-7xl px-6 pt-24 pb-16">
      <header className="mb-12 flex items-end justify-between gap-6">
        <div>
          <Link
            to="/forms"
            className="font-mono text-xs uppercase tracking-wider text-muted hover:text-accent"
          >
            ← Forms
          </Link>
          <h1 className="mt-2 font-display text-5xl font-bold leading-[1.0] text-ink">
            Connections
          </h1>
          {connections ? (
            <p className="mt-4 font-mono text-xs uppercase tracking-wider text-muted">
              {connections.length}{" "}
              {connections.length === 1 ? "connection" : "connections"}
            </p>
          ) : null}
        </div>
        <button
          type="button"
          onClick={() => setEditing("new")}
          className="shrink-0 bg-ink px-4 py-2 font-mono text-xs uppercase tracking-wider text-bg transition-colors hover:bg-accent-hover"
        >
          New connection
        </button>
      </header>

      {isLoading ? (
        <p className="font-display text-2xl text-ink opacity-30">Loading…</p>
      ) : error ? (
        <div className="border border-error bg-surface p-6">
          <p className="text-sm text-error">
            Couldn't load connections:{" "}
            {error instanceof ApiError ? error.message : "unknown error"}
          </p>
        </div>
      ) : connections && connections.length === 0 ? (
        <p className="text-sm text-muted">
          No connections yet. Add one to let workflow operators reach an
          Airflow instance.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr className="border-b border-border">
                <Th>Name</Th>
                <Th>Type</Th>
                <Th>Base URL</Th>
                <Th>Auth</Th>
                <Th>Updated</Th>
              </tr>
            </thead>
            <tbody>
              {connections?.map((conn) => (
                <tr
                  key={conn.name}
                  onClick={() => setEditing(conn)}
                  className="cursor-pointer border-b border-border transition-colors hover:bg-surface"
                >
                  <td className="py-4 pr-6 align-top font-medium text-ink">
                    {conn.name}
                  </td>
                  <td className="py-4 pr-6 align-top font-mono text-xs uppercase tracking-wider text-muted">
                    {connectionType(conn.conn_type)?.label ?? conn.conn_type}
                  </td>
                  <td className="py-4 pr-6 align-top font-mono text-sm text-ink">
                    {conn.base_url}
                  </td>
                  <td className="py-4 pr-6 align-top font-mono text-xs uppercase tracking-wider text-muted">
                    {conn.auth_kind}
                  </td>
                  <td className="py-4 align-top font-mono text-xs text-muted">
                    {formatTimestamp(conn.updated_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editing !== null ? (
        <ConnectionEditor
          target={editing}
          onClose={() => setEditing(null)}
        />
      ) : null}
    </main>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return (
    <th className="py-2 pr-6 text-left font-mono text-[11px] font-medium uppercase tracking-wider text-muted">
      {children}
    </th>
  );
}

// --- Editor ----------------------------------------------------------------

function ConnectionEditor({
  target,
  onClose,
}: {
  target: "new" | Connection;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const isNew = target === "new";
  const existing = isNew ? null : target;

  const [connType, setConnType] = useState(
    existing?.conn_type ?? CONNECTION_TYPES[0].id,
  );
  const [name, setName] = useState(existing?.name ?? "");
  const [baseUrl, setBaseUrl] = useState(existing?.base_url ?? "");
  const [authKind, setAuthKind] = useState<AuthKind>(
    existing?.auth_kind ?? CONNECTION_TYPES[0].authKinds[0],
  );
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  // AWS auth fields — credentials are write-only (kept blank on edit),
  // region is metadata so we may want to surface it on read later.
  const [awsAccessKeyId, setAwsAccessKeyId] = useState("");
  const [awsSecretAccessKey, setAwsSecretAccessKey] = useState("");
  const [awsSessionToken, setAwsSessionToken] = useState("");
  const [awsRegion, setAwsRegion] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: (body: ConnectionInput) => saveConnection(name.trim(), body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["connections"] });
      onClose();
    },
    onError: (err) => {
      setFormError(
        err instanceof ApiError ? err.message : "Couldn't save connection",
      );
    },
  });

  const remove = useMutation({
    mutationFn: () => deleteConnection(existing!.name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["connections"] });
      onClose();
    },
    onError: (err) => {
      setFormError(
        err instanceof ApiError ? err.message : "Couldn't delete connection",
      );
    },
  });

  function handleSave() {
    setFormError(null);
    if (isNew && !name.trim()) {
      setFormError("A connection needs a name.");
      return;
    }
    const typeConfig = connectionType(connType);
    const needsBaseUrl = typeConfig?.needsBaseUrl ?? true;
    if (needsBaseUrl && !baseUrl.trim()) {
      setFormError("A connection needs a base URL.");
      return;
    }
    const body: ConnectionInput = {
      conn_type: connType,
      base_url: needsBaseUrl ? baseUrl.trim() : "",
      auth_kind: authKind,
    };
    // Only send credentials when the fields were filled — blank means
    // "keep the stored secret" on an existing connection.
    if (authKind === "basic") {
      if (username || password) {
        body.username = username;
        body.password = password;
      }
    } else if (authKind === "token") {
      if (token) {
        body.token = token;
      }
    } else {
      // aws — access key + secret are the credential pair; session
      // token and region are optional. We send region whenever it
      // changed (it's not a secret per se but lives in the secret blob
      // today). On edit with blank credentials we still send region
      // if it was edited — but since we don't currently echo it back
      // on read, treat it as "set on create, leave alone on edit"
      // unless the user types into the field.
      if (awsAccessKeyId || awsSecretAccessKey) {
        body.aws_access_key_id = awsAccessKeyId;
        body.aws_secret_access_key = awsSecretAccessKey;
        if (awsSessionToken) {
          body.aws_session_token = awsSessionToken;
        }
        if (awsRegion) {
          body.aws_region = awsRegion;
        }
      }
    }
    save.mutate(body);
  }

  const busy = save.isPending || remove.isPending;
  const credentialHint = isNew
    ? "Required for a new connection."
    : "Leave blank to keep the stored credentials.";

  return (
    <section className="mt-10 border border-border bg-surface p-6">
      <h2 className="font-display text-xl font-bold text-ink">
        {isNew ? "New connection" : `Edit — ${existing!.name}`}
      </h2>

      <div className="mt-5 flex max-w-xl flex-col gap-4">
        <Field label="Name">
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={!isNew}
            spellCheck={false}
            placeholder="prod_airflow"
            className="w-full border border-border bg-bg px-3 py-2 font-mono text-sm text-ink disabled:opacity-50"
          />
        </Field>
        {!isNew ? (
          <p className="-mt-2 font-mono text-[11px] text-muted">
            The name is the identifier operators wire against — it can't
            be changed.
          </p>
        ) : null}

        <Field label="Type">
          <select
            value={connType}
            onChange={(e) => {
              const next = e.target.value;
              setConnType(next);
              // Keep auth valid for the chosen type.
              const kinds = connectionType(next)?.authKinds ?? [];
              if (kinds.length > 0 && !kinds.includes(authKind)) {
                setAuthKind(kinds[0]);
              }
            }}
            className="w-full border border-border bg-bg px-3 py-2 font-mono text-sm text-ink"
          >
            {CONNECTION_TYPES.map((t) => (
              <option key={t.id} value={t.id}>
                {t.label}
              </option>
            ))}
          </select>
        </Field>
        <p className="-mt-2 font-mono text-[11px] text-muted">
          {connectionType(connType)?.description}
        </p>

        {(connectionType(connType)?.needsBaseUrl ?? true) ? (
          <Field label="Base URL">
            <input
              type="text"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              spellCheck={false}
              placeholder="https://airflow.example.com"
              className="w-full border border-border bg-bg px-3 py-2 font-mono text-sm text-ink"
            />
          </Field>
        ) : null}

        <Field label="Authentication">
          <div className="flex gap-2">
            {(connectionType(connType)?.authKinds ?? []).map((kind) => (
              <button
                key={kind}
                type="button"
                onClick={() => setAuthKind(kind)}
                className={[
                  "border px-3 py-1.5 font-mono text-xs uppercase tracking-wider transition-colors",
                  authKind === kind
                    ? "border-ink bg-ink text-bg"
                    : "border-border text-muted hover:border-ink hover:text-ink",
                ].join(" ")}
              >
                {kind}
              </button>
            ))}
          </div>
        </Field>

        {authKind === "basic" ? (
          <>
            <Field label="Username">
              <input
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                spellCheck={false}
                autoComplete="off"
                className="w-full border border-border bg-bg px-3 py-2 font-mono text-sm text-ink"
              />
            </Field>
            <Field label="Password">
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="new-password"
                className="w-full border border-border bg-bg px-3 py-2 font-mono text-sm text-ink"
              />
            </Field>
          </>
        ) : authKind === "token" ? (
          <Field label="Token">
            <input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              autoComplete="new-password"
              className="w-full border border-border bg-bg px-3 py-2 font-mono text-sm text-ink"
            />
          </Field>
        ) : (
          <>
            <Field label="Access key ID">
              <input
                type="text"
                value={awsAccessKeyId}
                onChange={(e) => setAwsAccessKeyId(e.target.value)}
                spellCheck={false}
                autoComplete="off"
                placeholder="AKIA…"
                className="w-full border border-border bg-bg px-3 py-2 font-mono text-sm text-ink"
              />
            </Field>
            <Field label="Secret access key">
              <input
                type="password"
                value={awsSecretAccessKey}
                onChange={(e) => setAwsSecretAccessKey(e.target.value)}
                autoComplete="new-password"
                className="w-full border border-border bg-bg px-3 py-2 font-mono text-sm text-ink"
              />
            </Field>
            <Field label="Session token (optional)">
              <input
                type="password"
                value={awsSessionToken}
                onChange={(e) => setAwsSessionToken(e.target.value)}
                autoComplete="new-password"
                placeholder="For temporary credentials only"
                className="w-full border border-border bg-bg px-3 py-2 font-mono text-sm text-ink"
              />
            </Field>
            <Field label="Region">
              <input
                type="text"
                value={awsRegion}
                onChange={(e) => setAwsRegion(e.target.value)}
                spellCheck={false}
                autoComplete="off"
                placeholder="us-east-2"
                className="w-full border border-border bg-bg px-3 py-2 font-mono text-sm text-ink"
              />
            </Field>
          </>
        )}
        <p className="-mt-2 font-mono text-[11px] text-muted">
          {credentialHint} Credentials are encrypted at rest and never
          shown again.
        </p>

        {formError ? (
          <p className="border border-error bg-bg px-3 py-2 text-sm text-error">
            {formError}
          </p>
        ) : null}

        <div className="mt-2 flex items-center gap-3">
          <button
            type="button"
            onClick={handleSave}
            disabled={busy}
            className="bg-ink px-4 py-2 font-mono text-xs uppercase tracking-wider text-bg transition-colors hover:bg-accent-hover disabled:opacity-50"
          >
            {save.isPending ? "Saving…" : "Save"}
          </button>
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="border border-border px-4 py-2 font-mono text-xs uppercase tracking-wider text-ink transition-colors hover:border-ink disabled:opacity-50"
          >
            Cancel
          </button>
          {!isNew ? (
            <button
              type="button"
              onClick={() => remove.mutate()}
              disabled={busy}
              className="ml-auto border border-error px-4 py-2 font-mono text-xs uppercase tracking-wider text-error transition-colors hover:bg-error hover:text-bg disabled:opacity-50"
            >
              {remove.isPending ? "Deleting…" : "Delete"}
            </button>
          ) : null}
        </div>
      </div>
    </section>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="font-mono text-[11px] uppercase tracking-wider text-muted">
        {label}
      </span>
      {children}
    </label>
  );
}
