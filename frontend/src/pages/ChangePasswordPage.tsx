import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { changeOwnPassword, ApiError } from "../lib/api";

/**
 * Change-password screen. Serves two cases:
 *  - Forced change — a user whose password was reset by an admin
 *    (must_change_password). They are funnelled here and cannot reach
 *    the console until they set a new password.
 *  - Self-service — any signed-in user choosing to change it.
 * Both require the current password.
 */
export default function ChangePasswordPage() {
  const { user, refresh } = useAuth();
  const navigate = useNavigate();

  const forced = user?.must_change_password ?? false;

  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (next !== confirm) {
      setError("The new passwords don't match.");
      return;
    }
    if (next.length < 8) {
      setError("Use at least 8 characters.");
      return;
    }
    setBusy(true);
    try {
      await changeOwnPassword(current, next);
      await refresh();
      setDone(true);
      // Forced-change users land on the console once the flag clears.
      if (forced) {
        navigate("/forms", { replace: true });
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("Your current password is incorrect.");
      } else {
        setError("Couldn't change the password. Try again.");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-6">
      <h1 className="font-display text-3xl font-bold text-ink">
        {forced ? "Set a new password" : "Change password"}
      </h1>
      <p className="mt-2 text-sm text-muted">
        {forced
          ? "Your password was reset by an administrator. Choose a new one to continue."
          : "Update the password for your account."}
      </p>

      {done && !forced ? (
        <p className="mt-8 text-sm text-ink">
          Your password has been changed.
        </p>
      ) : (
        <form onSubmit={onSubmit} className="mt-8 flex flex-col gap-4">
          <label className="flex flex-col gap-1">
            <span className="font-mono text-[11px] uppercase tracking-[0.18em] text-muted">
              Current password
            </span>
            <input
              type="password"
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
              autoFocus
              required
              className="border border-border bg-surface px-3 py-2 text-ink focus:border-ink focus:outline-none"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="font-mono text-[11px] uppercase tracking-[0.18em] text-muted">
              New password
            </span>
            <input
              type="password"
              value={next}
              onChange={(e) => setNext(e.target.value)}
              required
              className="border border-border bg-surface px-3 py-2 text-ink focus:border-ink focus:outline-none"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="font-mono text-[11px] uppercase tracking-[0.18em] text-muted">
              Confirm new password
            </span>
            <input
              type="password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              required
              className="border border-border bg-surface px-3 py-2 text-ink focus:border-ink focus:outline-none"
            />
          </label>

          {error && <p className="text-sm text-error">{error}</p>}

          <button
            type="submit"
            disabled={busy}
            className="mt-2 border border-ink bg-ink px-4 py-2 font-mono text-[11px] uppercase tracking-[0.2em] text-bg transition-opacity hover:opacity-80 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy ? "Saving…" : "Change password"}
          </button>
        </form>
      )}

      {!forced && (
        <button
          onClick={() => navigate("/forms")}
          className="mt-6 self-start font-mono text-xs uppercase tracking-wider text-muted hover:text-accent"
        >
          ← Back to forms
        </button>
      )}
    </main>
  );
}
