"""Connection resolvers — a uniform way to look up stored credentials
for each external integration.

A `FrontFlowConnection` subclass represents *one kind* of external
connection (an Airflow instance, an AWS account, etc.). Subclasses are
type-level: they have no instance state. They expose two things:

  - `DEFAULT_NAME` — the conventional name in the connection store
    that's used when an operator or input doesn't specify one (e.g.
    `airflow_default`, `aws_default`).
  - `resolve(name)` — look up `name` in the store and return its
    record, or call `handle_missing_connection(name)` if not present.
    The base implementation of `handle_missing_connection` raises.
    A subclass can override to do something else — AWS overrides it
    to return None so the caller falls through to boto3's default
    credential chain.

This hierarchy is not exposed in the form-author DSL — workflow
authors pass connection *names* (strings) to operators. The resolvers
are internal infrastructure the runtime and the upload code use.

Distinct from the SQLAlchemy ORM `Connection` model in `store.py`,
which represents one row in the connection table. The resolver
classes here represent the *type* of connection and how it's looked
up; the ORM class represents one *instance* persisted in the DB.
"""

from __future__ import annotations
from typing import Any, Optional


class ConnectionResolutionError(Exception):
    """Raised when a required connection cannot be resolved — either
    the explicit name isn't in the store, or the conventional default
    name isn't either and the subclass's `handle_missing_connection`
    doesn't supply a fallback."""


class FrontFlowConnection:
    """Base resolver for one kind of external connection.

    Subclass per integration. Set `DEFAULT_NAME` and, if the
    integration has a meaningful no-connection fallback (like AWS's
    boto3 default chain), override `handle_missing_connection`.
    """

    # The conventional name in the connection store that's used when
    # an operator doesn't specify one. Override per subclass.
    DEFAULT_NAME: str = ""
    # The connection-store `conn_type` rows of this kind have. Used
    # for sanity-checking when a name is resolved.
    CONN_TYPE: str = ""

    @classmethod
    def resolve(cls, name: Optional[str]) -> Optional[dict[str, Any]]:
        """Resolve a connection by name to its stored record.

        `name=None` resolves the subclass's `DEFAULT_NAME`. A missing
        connection delegates to `handle_missing_connection`, which by
        default raises — a subclass can override to return a fallback
        (e.g. None for AWS, meaning "use boto3's default chain").
        Returns the credential record dict.
        """
        # Imported lazily — connections.py is itself imported during
        # DSL initialisation, before the store module is wired up.
        from . import store

        lookup_name = name if name is not None else cls.DEFAULT_NAME
        if not lookup_name:
            return cls.handle_missing_connection(lookup_name)
        rec = store.get_connection(lookup_name)
        if rec is None:
            return cls.handle_missing_connection(lookup_name)
        return rec

    @classmethod
    def handle_missing_connection(
        cls, name: str,
    ) -> Optional[dict[str, Any]]:
        """Called when `resolve(name)` finds nothing in the store.

        Base: raise `ConnectionResolutionError`. A subclass overrides
        this when its integration has a meaningful fallback path —
        AWS, for example, returns None so the caller can fall back to
        boto3's default credential chain (env vars, IAM, ~/.aws/...).
        """
        raise ConnectionResolutionError(
            f"connection {name!r} is not configured — add it to the "
            f"connection store, or pass `connection=` explicitly"
        )


class AirflowConnection(FrontFlowConnection):
    """The Airflow REST API connection resolver.

    Missing connection → error. Airflow has no notion of "ambient
    credentials" the way AWS does; if the named connection isn't
    stored, there's no meaningful fallback, so we fail loudly.
    """

    DEFAULT_NAME = "airflow_default"
    CONN_TYPE = "airflow"


class AWSConnection(FrontFlowConnection):
    """The AWS credentials resolver, used by `S3File`.

    Missing connection → return None. boto3 will then resolve
    credentials from its own default chain (environment variables,
    `~/.aws/credentials`, an instance/role profile, etc.). This
    matches how AWS-native tooling expects to work and avoids
    reimplementing what boto3 already does.
    """

    DEFAULT_NAME = "aws_default"
    CONN_TYPE = "aws"

    @classmethod
    def handle_missing_connection(
        cls, name: str,
    ) -> Optional[dict[str, Any]]:
        # Intentional: AWS has a meaningful no-connection fallback —
        # boto3's default credential chain. The caller (`_resolve_aws`
        # in uploads.py) treats a None return as "let boto3 do it".
        return None
