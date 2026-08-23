"""A thin, synchronous client over Superset's REST API.

Ported from the superset-form prototype, which was async; frontflow is
sync throughout, so this uses `httpx.Client`.

Credentials come from the connection store — a `superset` connection
whose `base_url` is the Superset URL and whose Fernet-encrypted secret
carries the service account. Nothing here reads the environment.

Endpoints used (verified against the pinned Superset build):
  POST /api/v1/security/login
  GET  /api/v1/security/csrf_token/
  GET  /api/v1/security/guest_token/           (POST — mint an embed token)
  GET  /api/v1/dashboard/
  POST /api/v1/dashboard/                      create a blank dashboard
  GET  /api/v1/dashboard/{id_or_slug}
  PUT  /api/v1/dashboard/{id}                  write json_metadata
  GET  /api/v1/dashboard/{id_or_slug}/embedded -> {"result": {"uuid", ...}}
  POST /api/v1/dashboard/{id_or_slug}/embedded -> {"result": {"uuid", ...}}
  GET  /api/v1/dataset/                        find the submissions dataset
  POST /api/v1/dataset/                        create it if absent
  GET  /api/v1/database/                       resolve the database id
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

from ..dsl.connections import SupersetConnection

logger = logging.getLogger(__name__)

# Refresh the access token this many seconds before it actually expires,
# so a request in flight cannot land on a just-expired token.
_TOKEN_EXPIRY_MARGIN = 30.0
_DEFAULT_TOKEN_TTL = 300.0

_DEFAULT_TIMEOUT = 30.0


class SupersetError(RuntimeError):
    """Superset was reachable but refused or failed the request."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SupersetUnreachable(RuntimeError):
    """Superset could not be contacted at all."""


def _require_httpx():
    """Import httpx, or explain how to get it.

    Superset support is an optional extra. Failing here with an install
    hint beats an ImportError at package load, which would break installs
    that never touch Superset.
    """
    try:
        import httpx  # noqa: PLC0415
    except ModuleNotFoundError as exc:  # pragma: no cover - env dependent
        raise SupersetError(
            "Superset support requires httpx — install it with "
            "`pip install frontflow[superset]`"
        ) from exc
    return httpx


class SupersetClient:
    """One client per unit of work; open it as a context manager.

    The access token is cached per (base_url, username) across
    instances, since logging in is the expensive part. CSRF is *not*
    cached that way — it is bound to the session cookie in one client's
    jar, so it lives on the instance.
    """

    # (base_url, username) -> (token, expires_at_monotonic)
    _token_cache: dict[tuple[str, str], tuple[str, float]] = {}

    def __init__(self, connection: Optional[str] = None) -> None:
        httpx = _require_httpx()

        record = SupersetConnection.resolve(connection)
        if not record:
            raise SupersetError(
                f"Superset connection {connection or SupersetConnection.DEFAULT_NAME!r} "
                "is not configured"
            )

        self.connection_name: str = record["name"]
        self.base_url: str = str(record["base_url"]).rstrip("/")

        secret = record.get("secret") or {}
        self._username: str = secret.get("username", "")
        self._password: str = secret.get("password", "")
        if not self._username:
            raise SupersetError(
                f"Superset connection {self.connection_name!r} has no username; "
                "Superset needs a service account to mint guest tokens."
            )

        self._http = httpx.Client(timeout=_DEFAULT_TIMEOUT)
        self._httpx = httpx
        # CSRF and its session cookie travel together and belong to this
        # client's cookie jar — never cache them process-wide.
        self._csrf_token: Optional[str] = None
        self._session_cookie: Optional[str] = None

    # -- lifecycle ----------------------------------------------------------

    def __enter__(self) -> "SupersetClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    @property
    def _cache_key(self) -> tuple[str, str]:
        return (self.base_url, self._username)

    # -- auth ---------------------------------------------------------------

    def _login(self) -> str:
        try:
            response = self._http.post(
                f"{self.base_url}/api/v1/security/login",
                json={
                    "username": self._username,
                    "password": self._password,
                    "provider": "db",
                    "refresh": True,
                },
            )
        except self._httpx.HTTPError as exc:
            raise SupersetUnreachable(str(exc)) from exc

        if response.status_code != 200:
            raise SupersetError(
                f"Superset rejected the credentials on connection "
                f"{self.connection_name!r} ({response.status_code}).",
                response.status_code,
            )

        token = response.json()["access_token"]
        SupersetClient._token_cache[self._cache_key] = (
            token,
            time.monotonic() + _DEFAULT_TOKEN_TTL - _TOKEN_EXPIRY_MARGIN,
        )
        return token

    def access_token(self, force: bool = False) -> str:
        if not force:
            cached = SupersetClient._token_cache.get(self._cache_key)
            if cached and time.monotonic() < cached[1]:
                return cached[0]
        return self._login()

    def csrf_token(self, bearer: str) -> Optional[str]:
        """Fetch a CSRF token, which Superset requires on mutating calls.

        The token is tied to the session cookie Superset sets on this
        response, so the same client — and therefore the same cookie jar
        — must make the follow-up request.
        """
        if self._csrf_token:
            return self._csrf_token

        try:
            response = self._http.get(
                f"{self.base_url}/api/v1/security/csrf_token/",
                headers={"Authorization": f"Bearer {bearer}"},
            )
        except self._httpx.HTTPError as exc:
            raise SupersetUnreachable(str(exc)) from exc

        if response.status_code != 200:
            # Not fatal: an install with WTF_CSRF_ENABLED = False does not
            # serve this, and its mutating endpoints accept requests
            # without a token.
            logger.info(
                "CSRF token unavailable (%s); proceeding without one.",
                response.status_code,
            )
            return None

        self._csrf_token = response.json().get("result")

        # Superset ties the CSRF token to the session cookie set on this
        # response and rejects the pair if either is missing.
        #
        # We read that cookie out of the jar and replay it as an explicit
        # header rather than letting httpx send it. A deployment sets
        # SESSION_COOKIE_SECURE = True (required so browsers accept the
        # SameSite=None cookie that in-page dashboard editing depends
        # on), and httpx correctly refuses to send a Secure cookie over a
        # plain-HTTP internal network. The cookie is still *stored*, just
        # not sent — so replaying it by hand is what lets both the
        # browser and this server-to-server path work at once.
        self._session_cookie = self._http.cookies.get("session")
        return self._csrf_token

    def request(self, method: str, path: str, **kwargs: Any):
        """Authenticated request, retrying once if a cached token is stale.

        Superset restarting invalidates tokens we still consider fresh,
        so a 401 is retried with a newly minted one before surfacing.
        """
        mutating = method.upper() not in ("GET", "HEAD", "OPTIONS")

        def _send(bearer: str):
            headers = {
                "Authorization": f"Bearer {bearer}",
                **kwargs.pop("headers", {}),
            }
            if mutating:
                csrf = self.csrf_token(bearer)
                if csrf:
                    headers["X-CSRFToken"] = csrf
                    headers.setdefault("Referer", self.base_url)
                    if self._session_cookie:
                        headers["Cookie"] = f"session={self._session_cookie}"
            try:
                return self._http.request(
                    method, f"{self.base_url}{path}", headers=headers, **kwargs
                )
            except self._httpx.HTTPError as exc:
                raise SupersetUnreachable(str(exc)) from exc

        response = _send(self.access_token())
        if response.status_code == 401:
            logger.info("Cached Superset access token rejected; re-authenticating.")
            self._csrf_token = None
            self._session_cookie = None
            response = _send(self.access_token(force=True))
        return response

    # -- status -------------------------------------------------------------

    def ping(self) -> dict[str, Any]:
        """Reachability plus whether the service account can authenticate."""
        try:
            health = self._http.get(f"{self.base_url}/health")
        except self._httpx.HTTPError as exc:
            raise SupersetUnreachable(str(exc)) from exc

        result: dict[str, Any] = {
            "reachable": health.status_code == 200,
            "authenticated": False,
            "username": None,
        }

        self.access_token(force=True)
        result["authenticated"] = True

        # Best effort; a missing /me endpoint is not worth failing a ping.
        try:
            me = self.request("GET", "/api/v1/me/")
            if me.status_code == 200:
                result["username"] = me.json().get("result", {}).get("username")
        except (SupersetError, SupersetUnreachable):
            pass

        return result

    # -- guest tokens -------------------------------------------------------

    def guest_token(
        self,
        embed_uuid: str,
        *,
        username: str = "frontflow-guest",
        first_name: str = "FrontFlow",
        last_name: str = "Guest",
        rls: Optional[list[dict[str, Any]]] = None,
    ) -> str:
        """Mint a guest token scoped to one embedded dashboard."""
        response = self.request(
            "POST",
            "/api/v1/security/guest_token/",
            json={
                # Must be the embed UUID, not the numeric dashboard id.
                "resources": [{"type": "dashboard", "id": embed_uuid}],
                "rls": rls or [],
                "user": {
                    "username": username,
                    "first_name": first_name,
                    "last_name": last_name,
                },
            },
        )
        if response.status_code != 200:
            raise SupersetError(
                f"Superset refused to issue a guest token "
                f"({response.status_code}): {response.text[:200]}",
                response.status_code,
            )
        return response.json()["token"]

    # -- discovery ----------------------------------------------------------

    def list_dashboards(self) -> list[dict[str, Any]]:
        query = json.dumps(
            {"columns": ["id", "dashboard_title", "status"], "page_size": 100}
        )
        response = self.request("GET", f"/api/v1/dashboard/?q={query}")
        if response.status_code != 200:
            raise SupersetError(
                f"Could not list dashboards ({response.status_code}).",
                response.status_code,
            )
        return [
            {
                "id": str(item.get("id")),
                "title": item.get("dashboard_title") or f"Dashboard {item.get('id')}",
                "status": item.get("status"),
            }
            for item in response.json().get("result", [])
        ]

    def get_embedded_uuid(self, dashboard_id: str) -> Optional[str]:
        """The dashboard's embed UUID, or None if embedding is off."""
        response = self.request("GET", f"/api/v1/dashboard/{dashboard_id}/embedded")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise SupersetError(
                f"Could not read the embed config ({response.status_code}).",
                response.status_code,
            )
        # Superset returns 200 with an empty result when embedding is off.
        result = response.json().get("result") or {}
        if isinstance(result, list):
            result = result[0] if result else {}
        return result.get("uuid") or None

    def enable_embedding(
        self, dashboard_id: str, allowed_domains: list[str]
    ) -> str:
        """Turn embedding on (or update allowed domains); return the UUID."""
        response = self.request(
            "POST",
            f"/api/v1/dashboard/{dashboard_id}/embedded",
            json={"allowed_domains": allowed_domains},
        )
        if response.status_code not in (200, 201):
            raise SupersetError(
                f"Could not enable embedding ({response.status_code}): "
                f"{response.text[:200]}",
                response.status_code,
            )
        embed_uuid = (response.json().get("result") or {}).get("uuid")
        if not embed_uuid:
            raise SupersetError("Superset did not return an embed UUID.")
        return embed_uuid

    def list_native_filters(self, dashboard_id: str) -> list[dict[str, Any]]:
        """Native filters declared in the dashboard's JSON metadata.

        This is where the filter id that `RefreshDashboard` drives comes
        from — reading it here beats asking someone to dig it out of a URL.
        """
        metadata = self.get_json_metadata(dashboard_id)
        filters = []
        for item in metadata.get("native_filter_configuration") or []:
            filter_type = item.get("filterType") or ""
            targets = item.get("targets") or [{}]
            column = (targets[0] or {}).get("column", {}).get("name")
            filters.append(
                {
                    "id": item.get("id"),
                    "name": item.get("name") or item.get("id"),
                    "filter_type": filter_type,
                    "column": column,
                    # Surfaced so callers can pick a drivable filter
                    # without hardcoding Superset's type constants.
                    "is_time": "time" in filter_type.lower(),
                }
            )
        return filters

    def ensure_calculated_column(
        self,
        dataset_id: int,
        column_name: str,
        expression: str,
        column_type: str,
    ) -> None:
        """Add a calculated column to a dataset, unless it has one.

        Matched case-insensitively: `Region` and `region` are the same
        column to a person, and creating both would leave two nearly
        identical entries in every column picker.

        A PUT replaces the whole column list, so the existing ones are
        read back and resent — dropping them would delete the dataset's
        columns, not leave them alone.
        """
        detail = self.request("GET", f"/api/v1/dataset/{dataset_id}").json()
        columns = detail["result"]["columns"]

        wanted = column_name.strip().lower()
        if any(c["column_name"].strip().lower() == wanted for c in columns):
            return

        keep = (
            "id", "column_name", "type", "expression", "is_dttm",
            "filterable", "groupby", "verbose_name", "description",
        )
        payload = [
            {k: v for k, v in c.items() if k in keep} for c in columns
        ] + [
            {
                "column_name": column_name,
                "type": column_type,
                "expression": expression,
                "filterable": True,
                "groupby": True,
                "is_dttm": column_type == "TIMESTAMP",
            }
        ]
        self.request(
            "PUT", f"/api/v1/dataset/{dataset_id}", json={"columns": payload}
        )

    def ensure_native_filter(
        self,
        dashboard_id: str,
        dataset_id: int,
        name: str,
        column: str,
        filter_type: str,
    ) -> str:
        """Add a native filter named `name`, unless the dashboard has one.

        Matched on NAME rather than column, because the name is what a
        `SetFilters` directive refers to — two filters sharing a name
        would make that reference ambiguous, which is worse than two
        filters on one column.
        """
        metadata = self.get_json_metadata(dashboard_id)
        existing = metadata.get("native_filter_configuration") or []

        wanted = name.strip().lower()
        for item in existing:
            if (item.get("name") or "").strip().lower() == wanted:
                return item.get("id") or ""

        filter_id = f"NATIVE_FILTER-{uuid.uuid4().hex[:12]}"
        existing.append(
            {
                "id": filter_id,
                "name": name,
                "filterType": filter_type,
                "type": "NATIVE_FILTER",
                "targets": [
                    {"datasetId": dataset_id, "column": {"name": column}}
                ],
                "defaultDataMask": {
                    "extraFormData": {},
                    "filterState": {},
                    "ownState": {},
                },
                "controlValues": {},
                "cascadeParentIds": [],
                # Every chart: a filter scoped to a subset would leave
                # the rest showing something the filter bar denies.
                "scope": {"rootPath": ["ROOT_ID"], "excluded": []},
                "chartsInScope": [],
                "tabsInScope": [],
                "description": "Declared in frontflow.",
            }
        )
        metadata["native_filter_configuration"] = existing
        self.set_json_metadata(dashboard_id, metadata)
        return filter_id

    # -- Explore access: real Superset accounts ----------------------------
    #
    # An embedded dashboard is served by an anonymous guest token, and
    # `rls` on that token restricts it. Explore cannot work that way —
    # a guest token grants dashboards only, and Superset rejects
    # modified query payloads from guest users — so a person who builds
    # their own charts needs a real Superset account, and the
    # restriction has to live in Superset's own RLS instead.

    def find_user(self, username: str) -> Optional[dict[str, Any]]:
        """A Superset user by username, or None."""
        response = self.request("GET", "/api/v1/security/users/")
        for user in response.json().get("result", []):
            if user.get("username") == username:
                return user
        return None

    def ensure_superset_user(
        self,
        username: str,
        *,
        email: str,
        role_ids: list[int],
        password: str,
        first_name: str = "",
        last_name: str = "",
    ) -> Optional[int]:
        """A Superset account mirroring a frontflow user.

        Idempotent by username. An existing account is left alone —
        including its roles: an administrator may have granted something
        deliberately, and silently resetting that on every login would
        make Superset's own user admin pointless.
        """
        existing = self.find_user(username)
        if existing is not None:
            return existing.get("id")

        response = self.request(
            "POST",
            "/api/v1/security/users/",
            json={
                "username": username,
                "first_name": first_name or username,
                "last_name": last_name or "User",
                "email": email,
                "active": True,
                "password": password,
                "roles": role_ids,
            },
        )
        return response.json().get("id")

    def find_role_id(self, name: str) -> Optional[int]:
        """A role's id by name, or None when it does not exist.

        Roles are NOT created here. This build exposes no endpoint for
        setting a role's permissions — `/api/v1/security/roles/` accepts
        only a name — so a role frontflow created would carry none. The
        read-only tiers have to be defined by an administrator inside
        Superset. See deploy/superset/bootstrap_roles.py.
        """
        response = self.request("GET", "/api/v1/security/roles/")
        for role in response.json().get("result", []):
            if role.get("name") == name:
                return role.get("id")
        return None

    def find_subject_for_user(self, user_id: int) -> Optional[int]:
        """The Subject id representing a Superset user.

        Superset models RLS targets as `Subject` — "a unified entity
        representing a User, Role, or Group" — and syncs one per user
        automatically. Targeting the USER subject is what lets each
        person have their own clause without a role per slice, and
        without Jinja in the clause (template processing is off by
        default, and an unrendered `{{ ... }}` would be spliced into SQL
        literally).
        """
        response = self.request("GET", "/api/v1/security/subject/")
        for subject in response.json().get("result", []):
            if subject.get("user_id") == user_id:
                return subject.get("id")
        return None

    def ensure_rls_rule(
        self,
        name: str,
        *,
        clause: str,
        table_ids: list[int],
        subject_ids: list[int],
        description: str = "Managed by frontflow.",
    ) -> Optional[int]:
        """Create or update a row-level security rule, keyed by name.

        Updated rather than duplicated: the clause a person is limited
        to changes when their access does, and a stale rule left beside
        a new one would widen it — Superset ORs a subject's rules
        together within a group, so an obsolete rule is not merely
        untidy, it is a hole.
        """
        response = self.request("GET", "/api/v1/rowlevelsecurity/")
        existing = {
            rule.get("name"): rule.get("id")
            for rule in response.json().get("result", [])
        }

        payload = {
            "name": name,
            "description": description,
            "filter_type": "Regular",
            "tables": table_ids,
            "subjects": subject_ids,
            "clause": clause,
        }

        rule_id = existing.get(name)
        if rule_id is not None:
            self.request(
                "PUT", f"/api/v1/rowlevelsecurity/{rule_id}", json=payload
            )
            return rule_id

        created = self.request(
            "POST", "/api/v1/rowlevelsecurity/", json=payload
        )
        return created.json().get("id")

    # -- provisioning -------------------------------------------------------

    def create_dashboard(self, title: str) -> str:
        """Create a blank dashboard; return its numeric id."""
        response = self.request(
            "POST",
            "/api/v1/dashboard/",
            json={"dashboard_title": title, "published": True},
        )
        if response.status_code not in (200, 201):
            raise SupersetError(
                f"Could not create the dashboard ({response.status_code}): "
                f"{response.text[:200]}",
                response.status_code,
            )
        return str(response.json()["id"])

    def delete_dashboard(self, dashboard_id: str) -> None:
        response = self.request("DELETE", f"/api/v1/dashboard/{dashboard_id}")
        if response.status_code not in (200, 404):
            raise SupersetError(
                f"Could not delete the dashboard ({response.status_code}).",
                response.status_code,
            )

    def get_database_id(self, name: str) -> Optional[int]:
        query = json.dumps(
            {"filters": [{"col": "database_name", "opr": "eq", "value": name}]}
        )
        response = self.request("GET", f"/api/v1/database/?q={query}")
        if response.status_code != 200:
            return None
        results = response.json().get("result", [])
        return results[0]["id"] if results else None

    def find_dataset_id(self, table_name: str) -> Optional[int]:
        query = json.dumps(
            {"filters": [{"col": "table_name", "opr": "eq", "value": table_name}]}
        )
        response = self.request("GET", f"/api/v1/dataset/?q={query}")
        if response.status_code != 200:
            return None
        results = response.json().get("result", [])
        return results[0]["id"] if results else None

    def create_dataset(
        self, database_id: int, table_name: str, schema: str = "public"
    ) -> int:
        response = self.request(
            "POST",
            "/api/v1/dataset/",
            json={
                "database": database_id,
                "schema": schema,
                "table_name": table_name,
            },
        )
        if response.status_code not in (200, 201):
            raise SupersetError(
                f"Could not create the dataset ({response.status_code}): "
                f"{response.text[:200]}",
                response.status_code,
            )
        return int(response.json()["id"])

    def ensure_dataset(
        self, table_name: str, database_name: str, schema: str = "public"
    ) -> Optional[int]:
        """Find the dataset, creating it if Superset does not have it."""
        existing = self.find_dataset_id(table_name)
        if existing is not None:
            return existing

        database_id = self.get_database_id(database_name)
        if database_id is None:
            logger.warning(
                "Database %r is not registered in Superset; cannot create the "
                "%r dataset automatically.",
                database_name,
                table_name,
            )
            return None
        return self.create_dataset(database_id, table_name, schema)

    def get_json_metadata(self, dashboard_id: str) -> dict[str, Any]:
        response = self.request("GET", f"/api/v1/dashboard/{dashboard_id}")
        if response.status_code != 200:
            raise SupersetError(
                f"Could not read the dashboard ({response.status_code}).",
                response.status_code,
            )
        raw = (response.json().get("result") or {}).get("json_metadata")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            logger.warning(
                "Dashboard %s has unparseable json_metadata", dashboard_id
            )
            return {}

    def set_json_metadata(
        self, dashboard_id: str, metadata: dict[str, Any]
    ) -> None:
        response = self.request(
            "PUT",
            f"/api/v1/dashboard/{dashboard_id}",
            json={"json_metadata": json.dumps(metadata)},
        )
        if response.status_code not in (200, 201):
            raise SupersetError(
                f"Could not write the dashboard metadata "
                f"({response.status_code}): {response.text[:200]}",
                response.status_code,
            )

    def ensure_time_filter(
        self,
        dashboard_id: str,
        dataset_id: int,
        column: str = "created_at",
        name: str = "Live refresh",
    ) -> Optional[str]:
        """Add a time-range native filter on `column`, unless one exists.

        This is the filter `RefreshDashboard` drives. Creating it here is
        what lets an auto-provisioned dashboard support live updates with
        no manual setup in Superset.
        """
        metadata = self.get_json_metadata(dashboard_id)
        existing = metadata.get("native_filter_configuration") or []

        for item in existing:
            targets = item.get("targets") or [{}]
            target_column = (targets[0] or {}).get("column", {}).get("name")
            if (
                "time" in (item.get("filterType") or "").lower()
                and target_column == column
            ):
                return item.get("id")

        filter_id = f"NATIVE_FILTER-{uuid.uuid4().hex[:12]}"
        existing.append(
            {
                "id": filter_id,
                "name": name,
                "filterType": "filter_time",
                "type": "NATIVE_FILTER",
                "targets": [
                    {"datasetId": dataset_id, "column": {"name": column}}
                ],
                "defaultDataMask": {
                    "extraFormData": {},
                    "filterState": {},
                    "ownState": {},
                },
                "controlValues": {},
                "scope": {"rootPath": ["ROOT_ID"], "excluded": []},
                # Applies to every chart: a filter scoped to a subset
                # would leave some charts stale after a refresh.
                "chartsInScope": [],
                "tabsInScope": [],
                "description": "Driven by frontflow to refresh charts in place.",
            }
        )
        metadata["native_filter_configuration"] = existing
        self.set_json_metadata(dashboard_id, metadata)
        return filter_id
