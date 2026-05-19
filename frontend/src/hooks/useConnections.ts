import { useQuery } from "@tanstack/react-query";
import { listConnections, type Connection } from "../lib/api";

/**
 * Fetch the connection store — every stored credentialed endpoint.
 * Metadata only; credentials are never sent to the client.
 */
export function useConnections() {
  return useQuery<Connection[]>({
    queryKey: ["connections"],
    queryFn: listConnections,
    refetchOnWindowFocus: true,
    staleTime: 30_000,
  });
}
