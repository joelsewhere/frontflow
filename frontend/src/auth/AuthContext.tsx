import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { Navigate, useLocation } from "react-router-dom";
import {
  fetchCurrentUser,
  login as apiLogin,
  logout as apiLogout,
  type UserInfo,
} from "../lib/api";

interface AuthState {
  /** The signed-in user, or null. Undefined while the initial
   *  /auth/me check is still in flight. */
  user: UserInfo | null | undefined;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  /** Re-fetch the current user — e.g. after a password change clears
   *  the forced-change flag. */
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserInfo | null | undefined>(
    undefined,
  );

  // On mount, ask the backend who we are (the session cookie, if any,
  // rides along automatically).
  useEffect(() => {
    let cancelled = false;
    fetchCurrentUser()
      .then((u) => {
        if (!cancelled) setUser(u);
      })
      .catch(() => {
        if (!cancelled) setUser(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(
    async (username: string, password: string) => {
      const u = await apiLogin(username, password);
      setUser(u);
    },
    [],
  );

  const logout = useCallback(async () => {
    await apiLogout();
    setUser(null);
  }, []);

  const refresh = useCallback(async () => {
    try {
      setUser(await fetchCurrentUser());
    } catch {
      setUser(null);
    }
  }, []);

  return (
    <AuthContext.Provider value={{ user, login, logout, refresh }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (ctx === null) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return ctx;
}

/** Route guard for admin-only pages. Renders children only for a
 *  signed-in admin; a signed-in non-admin is sent to /forms, a
 *  signed-out user to /login. */
export function RequireAdmin({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const location = useLocation();

  if (user === undefined) {
    return (
      <div className="p-12 font-display text-2xl text-ink opacity-30">
        Loading…
      </div>
    );
  }
  if (user === null) {
    return (
      <Navigate to="/login" replace state={{ from: location.pathname }} />
    );
  }
  if (user.must_change_password) {
    return <Navigate to="/change-password" replace />;
  }
  if (!user.is_admin) {
    return <Navigate to="/forms" replace />;
  }
  return <>{children}</>;
}

/** Route guard for the admin console. Renders its children only for a
 *  signed-in user; otherwise redirects to /login, remembering where
 *  the user was headed so login can send them back. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const location = useLocation();

  if (user === undefined) {
    // The initial /auth/me check is still resolving.
    return (
      <div className="p-12 font-display text-2xl text-ink opacity-30">
        Loading…
      </div>
    );
  }
  if (user === null) {
    return (
      <Navigate to="/login" replace state={{ from: location.pathname }} />
    );
  }
  // A forced-change user is authenticated but funnelled — the console
  // stays out of reach until they set a new password.
  if (
    user.must_change_password &&
    location.pathname !== "/change-password"
  ) {
    return <Navigate to="/change-password" replace />;
  }
  return <>{children}</>;
}
