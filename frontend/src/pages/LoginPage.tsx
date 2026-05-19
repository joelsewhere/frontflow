import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { ApiError } from "../lib/api";

/** The admin console sign-in screen. On success, returns the user to
 *  wherever they were headed before the auth redirect. */
export default function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const from =
    (location.state as { from?: string } | null)?.from ?? "/forms";

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username, password);
      navigate(from, { replace: true });
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("Incorrect username or password.");
      } else if (err instanceof ApiError && err.status === 503) {
        setError(
          "No account exists yet — create one with the " +
            "`frontflow create-admin` command.",
        );
      } else {
        setError("Couldn't sign in. Is the server reachable?");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-6">
      <h1 className="font-display text-3xl font-bold text-ink">
        Sign in
      </h1>
      <p className="mt-2 text-sm text-muted">
        The frontflow admin console.
      </p>

      <form onSubmit={onSubmit} className="mt-8 flex flex-col gap-4">
        <label className="flex flex-col gap-1">
          <span className="font-mono text-[11px] uppercase tracking-[0.18em] text-muted">
            Username
          </span>
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
            required
            className="border border-border bg-surface px-3 py-2 text-ink
              focus:border-ink focus:outline-none"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="font-mono text-[11px] uppercase tracking-[0.18em] text-muted">
            Password
          </span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            className="border border-border bg-surface px-3 py-2 text-ink
              focus:border-ink focus:outline-none"
          />
        </label>

        {error && <p className="text-sm text-error">{error}</p>}

        <button
          type="submit"
          disabled={busy}
          className="mt-2 border border-ink bg-ink px-4 py-2 font-mono
            text-[11px] uppercase tracking-[0.2em] text-bg
            transition-opacity hover:opacity-80
            disabled:cursor-not-allowed disabled:opacity-40"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
