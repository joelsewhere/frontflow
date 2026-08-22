/**
 * Registry of connection types the console can create.
 *
 * The connection store itself is type-agnostic — `conn_type` is a free
 * string on the backend — so this registry is all the UI needs: adding
 * a new integration is a single entry here.
 *
 * Every type today shares the same shape — a base URL plus an auth
 * scheme — which fits any REST-style integration. When a future type
 * needs different fields, give its entry the extra config it requires
 * and have the editor branch on it; `authKinds` already works that way,
 * so a type that only does token auth (or adds new schemes) is a
 * registry change, not an editor change.
 */

export type AuthKind = "basic" | "token" | "aws";

export interface ConnectionType {
  /** Stored as `conn_type`; what operators key off. */
  id: string;
  label: string;
  description: string;
  /** Auth schemes this type supports — drives the editor's auth options. */
  authKinds: AuthKind[];
  /** Some integrations (AWS) have no per-instance URL — the endpoint is
   *  derived from the credentials + region. When false, the editor
   *  drops the base-URL field. */
  needsBaseUrl?: boolean;
}

export const CONNECTION_TYPES: ConnectionType[] = [
  {
    id: "airflow",
    label: "Airflow REST API",
    description:
      "An Apache Airflow instance reached over its REST API — workflow " +
      "operators trigger DAGs, poll tasks, and pull XComs through it.",
    authKinds: ["basic", "token"],
    needsBaseUrl: true,
  },
  {
    id: "superset",
    label: "Apache Superset",
    description:
      "A Superset instance reached over its REST API — dashboard blocks " +
      "mint guest tokens through it, and named dashboards are " +
      "provisioned there on first use. The service account needs rights " +
      "to create dashboards and datasets.",
    authKinds: ["basic"],
    needsBaseUrl: true,
  },
  {
    id: "aws",
    label: "AWS",
    description:
      "AWS credentials for S3 uploads, downloads, and @backend " +
      "S3Hook access. Provide an access key pair (plus an optional " +
      "session token for temporary credentials) and the bucket's " +
      "region. Without a connection, frontflow falls back to boto3's " +
      "default credential chain (env vars, ~/.aws/credentials, " +
      "instance profile).",
    authKinds: ["aws"],
    needsBaseUrl: false,
  },
];

/** Look up a type by id; undefined for an unrecognized stored type. */
export function connectionType(id: string): ConnectionType | undefined {
  return CONNECTION_TYPES.find((t) => t.id === id);
}
