import { useQuery } from "@tanstack/react-query";
import { listUserAssignments, type UserAssignment } from "../lib/api";

/**
 * Fetch every assignment ever granted to or revoked from one user.
 *
 * Backs the admin UserDetailPage's Access tab. `includeRevoked` mirrors
 * the backend query param — true (default) returns active + historical
 * rows, false filters to active-only. Admin-only on the server.
 *
 * staleTime is 0 so a revoke from the admin page's button immediately
 * refetches and renders the row as revoked, rather than serving the
 * pre-revoke cached snapshot.
 */
export function useUserAssignments(
  userId: number | undefined,
  includeRevoked: boolean = true,
) {
  return useQuery<UserAssignment[]>({
    queryKey: ["userAssignments", userId, includeRevoked],
    queryFn: () => listUserAssignments(userId as number, includeRevoked),
    enabled: userId !== undefined && !Number.isNaN(userId),
    refetchOnWindowFocus: true,
    refetchOnMount: "always",
    staleTime: 0,
  });
}
