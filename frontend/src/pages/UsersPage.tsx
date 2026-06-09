import { useEffect, useState, useCallback } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import {
  listUsers,
  createUser,
  resetUserPassword,
  setUserAdmin,
  setUserActive,
  deleteUser,
  ApiError,
  type AccountInfo,
} from "../lib/api";

/**
 * User management (`/users`, admin only) — create accounts, reset
 * passwords, toggle admin, activate/deactivate, and delete. Guardrails
 * (last admin, self-action) are enforced by the backend; their
 * messages surface inline here.
 */
export default function UsersPage() {
  const { user: me } = useAuth();
  const [users, setUsers] = useState<AccountInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  // create-user form
  const [newName, setNewName] = useState("");
  const [newPass, setNewPass] = useState("");
  const [newAdmin, setNewAdmin] = useState(false);

  const load = useCallback(async () => {
    try {
      setUsers(await listUsers());
    } catch {
      setError("Couldn't load users.");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  function reportError(err: unknown, fallback: string) {
    if (err instanceof ApiError && (err.status === 409 || err.status === 403)) {
      setError(err.message);
    } else {
      setError(fallback);
    }
  }

  async function onCreate() {
    setError(null);
    if (!newName.trim() || newPass.length < 8) {
      setError("Username required, and password of at least 8 characters.");
      return;
    }
    try {
      await createUser(newName.trim(), newPass, newAdmin);
      setNewName("");
      setNewPass("");
      setNewAdmin(false);
      await load();
    } catch (err) {
      reportError(err, "Couldn't create the user — name may be taken.");
    }
  }

  async function onReset(u: AccountInfo) {
    setError(null);
    const pw = window.prompt(
      `Set a temporary password for "${u.username}". They will be ` +
        `required to change it at next sign-in.`,
    );
    if (pw === null) return;
    if (pw.length < 8) {
      setError("Temporary password must be at least 8 characters.");
      return;
    }
    try {
      await resetUserPassword(u.id, pw);
      await load();
    } catch (err) {
      reportError(err, "Couldn't reset the password.");
    }
  }

  async function onToggleAdmin(u: AccountInfo) {
    setError(null);
    try {
      await setUserAdmin(u.id, !u.is_admin);
      await load();
    } catch (err) {
      reportError(err, "Couldn't change admin status.");
    }
  }

  async function onToggleActive(u: AccountInfo) {
    setError(null);
    try {
      await setUserActive(u.id, !u.is_active);
      await load();
    } catch (err) {
      reportError(err, "Couldn't change the account status.");
    }
  }

  async function onDelete(u: AccountInfo) {
    setError(null);
    if (
      !window.confirm(
        `Delete "${u.username}"? This removes the account, its group ` +
          `memberships, and its form permissions. This cannot be undone.`,
      )
    )
      return;
    try {
      await deleteUser(u.id);
      await load();
    } catch (err) {
      reportError(err, "Couldn't delete the user.");
    }
  }

  return (
    <main className="relative z-10 mx-auto max-w-5xl px-6 pt-24 pb-16">
      <header className="mb-10">
        <Link
          to="/forms"
          className="font-mono text-xs uppercase tracking-wider text-muted hover:text-accent"
        >
          ← Forms
        </Link>
        <h1 className="mt-4 font-display text-5xl font-bold text-ink">
          Users
        </h1>
        <p className="mt-3 max-w-xl text-sm text-muted">
          Accounts that can sign in to the console. A reset password is
          temporary — the user must choose a new one at next sign-in.
        </p>
      </header>

      {error && (
        <div className="mb-6 border border-error bg-surface p-4">
          <p className="text-sm text-error">{error}</p>
        </div>
      )}

      {/* Create */}
      <section className="mb-10 border border-border p-5">
        <h2 className="mb-3 font-mono text-[11px] uppercase tracking-[0.2em] text-muted">
          Add a user
        </h2>
        <div className="flex flex-wrap items-center gap-2">
          <input
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="Username"
            className="w-44 border border-border bg-surface px-3 py-2 text-sm text-ink focus:border-ink focus:outline-none"
          />
          <input
            type="password"
            value={newPass}
            onChange={(e) => setNewPass(e.target.value)}
            placeholder="Initial password"
            className="w-52 border border-border bg-surface px-3 py-2 text-sm text-ink focus:border-ink focus:outline-none"
          />
          <label className="flex items-center gap-2 px-2 text-sm text-ink">
            <input
              type="checkbox"
              checked={newAdmin}
              onChange={(e) => setNewAdmin(e.target.checked)}
            />
            Admin
          </label>
          <button
            onClick={onCreate}
            className="border border-ink bg-ink px-4 py-2 font-mono text-[10px] uppercase tracking-[0.18em] text-bg hover:opacity-80"
          >
            Create
          </button>
        </div>
      </section>

      {/* List */}
      <section>
        <h2 className="mb-3 font-mono text-[11px] uppercase tracking-[0.2em] text-muted">
          Accounts
        </h2>
        <ul className="flex flex-col">
          {users?.map((u) => {
            const isSelf = me?.username === u.username;
            return (
              <li
                key={u.id}
                className="flex flex-wrap items-center gap-3 border-b border-border py-3"
              >
                <span className="min-w-[10rem] font-mono text-sm text-ink">
                  {u.username}
                  {isSelf && (
                    <span className="ml-2 text-[10px] uppercase tracking-wider text-muted">
                      you
                    </span>
                  )}
                </span>
                <span className="flex gap-1.5">
                  {u.is_admin && (
                    <Tag>admin</Tag>
                  )}
                  {!u.is_active && <Tag tone="error">deactivated</Tag>}
                  {u.must_change_password && (
                    <Tag tone="muted">must change password</Tag>
                  )}
                </span>
                <span className="ml-auto flex flex-wrap gap-2">
                  <Link
                    to={`/users/${u.id}`}
                    className="font-mono text-[10px] uppercase tracking-[0.16em] text-muted hover:text-accent"
                  >
                    Access →
                  </Link>
                  <Action onClick={() => onReset(u)}>
                    Reset password
                  </Action>
                  <Action
                    onClick={() => onToggleAdmin(u)}
                    disabled={isSelf}
                  >
                    {u.is_admin ? "Revoke admin" : "Make admin"}
                  </Action>
                  <Action
                    onClick={() => onToggleActive(u)}
                    disabled={isSelf}
                  >
                    {u.is_active ? "Deactivate" : "Reactivate"}
                  </Action>
                  <Action
                    onClick={() => onDelete(u)}
                    disabled={isSelf}
                    tone="error"
                  >
                    Delete
                  </Action>
                </span>
              </li>
            );
          })}
          {users && users.length === 0 && (
            <li className="py-3 text-sm text-muted">No users.</li>
          )}
        </ul>
      </section>
    </main>
  );
}

function Tag({
  children,
  tone = "accent",
}: {
  children: React.ReactNode;
  tone?: "accent" | "muted" | "error";
}) {
  const cls =
    tone === "error"
      ? "border-error text-error"
      : tone === "muted"
        ? "border-border text-muted"
        : "border-accent text-accent";
  return (
    <span
      className={`border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.16em] ${cls}`}
    >
      {children}
    </span>
  );
}

function Action({
  children,
  onClick,
  disabled,
  tone,
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  tone?: "error";
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={[
        "font-mono text-[10px] uppercase tracking-[0.16em]",
        disabled
          ? "cursor-not-allowed text-border"
          : tone === "error"
            ? "text-muted hover:text-error"
            : "text-muted hover:text-accent",
      ].join(" ")}
    >
      {children}
    </button>
  );
}
