import { Outlet } from "react-router-dom";
import { AuthProvider } from "./auth/AuthContext";

export default function App() {
  return (
    <div className="grain min-h-screen">
      <AuthProvider>
        <Outlet />
      </AuthProvider>
    </div>
  );
}
