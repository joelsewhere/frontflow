import { useQuery } from "@tanstack/react-query";
import { listMyTasks, type MyTask } from "../lib/api";

/**
 * Fetch the signed-in user's assignment inbox — every active
 * SubmissionAssignment granted to them, newest first.
 *
 * staleTime is 0 and refetchOnMount is "always" so navigating back
 * to /my-tasks after revoking a task elsewhere shows the updated
 * list immediately rather than serving a stale cached result.
 */
export function useMyTasks() {
  return useQuery<MyTask[]>({
    queryKey: ["myTasks"],
    queryFn: listMyTasks,
    refetchOnWindowFocus: true,
    refetchOnMount: "always",
    staleTime: 0,
  });
}
