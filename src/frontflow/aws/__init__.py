"""AWS namespace — hooks for connecting to AWS services from `@backend`
functions and other places that need a credential-resolved boto3 client.

Mirrors the `frontflow.airflow` namespace: a thin re-export surface that
hides the internal module layout from workflow authors.
"""

from frontflow.aws import hooks  # noqa: F401 — surfaced as `frontflow.aws.hooks`

__all__ = ["hooks"]
