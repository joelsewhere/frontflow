import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../../auth/AuthContext";

/** A compact console control: the signed-in username, a change-password
 *  link, and a sign-out action. Renders nothing if there is no user. */
export function SignOutControl() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  if (!user) return null;

  async function onSignOut() {
    await logout();
    navigate("/login", { replace: true });
  }

  return (
    <span className="flex items-center gap-3 font-mono text-xs uppercase tracking-wider">
      <span className="text-muted">{user.username}</span>
      <Link
        to="/change-password"
        className="text-muted hover:text-accent"
      >
        Password
      </Link>
      <button
        type="button"
        onClick={onSignOut}
        className="text-muted hover:text-accent"
      >
        Sign out
      </button>
    </span>
  );
}
