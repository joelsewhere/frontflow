"""HTTP surface for Superset dashboards.

Deliberately split in two:

  * **Form-scoped** (`/api/forms/{form_id}/dashboards/{name}/…`) — what a
    dashboard block calls while someone fills in a form. Gated by the
    form's own visibility rules AND by the dashboard actually appearing
    in that form. Minting a guest token is handing out read access to a
    dashboard, so it must not be possible to name an arbitrary dashboard
    and get a token for it.

  * **Admin** (`/api/dashboards…`) — listing and repairing bindings.

The prototype this was ported from left its guest-token endpoint
unauthenticated, which was a documented local-development compromise.
frontflow has real users, groups, and per-form ACLs, so that would be a
regression here; every route below is gated.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException

from ..dsl import auth, store
from ..dsl.compile import CompiledBlock
from . import provisioning, rls
from .client import SupersetClient, SupersetError, SupersetUnreachable

logger = logging.getLogger(__name__)

router = APIRouter()


def _translate(exc: Exception) -> HTTPException:
    """Map client failures onto responses a dashboard block can render."""
    if isinstance(exc, SupersetUnreachable):
        return HTTPException(
            status_code=503, detail=f"Could not reach Superset: {exc}"
        )
    if isinstance(exc, SupersetError):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def _public_superset_url(fallback: str) -> str:
    """The Superset URL the *browser* should use for SESSION surfaces —
    Explore, the dashboard editor, new-chart.

    Distinct from the connection's `base_url`, which is how this server
    reaches Superset — often an internal hostname (`http://superset:8088`)
    that means nothing in a browser. Falls back to the connection URL,
    which is correct for single-host development.
    """
    return os.environ.get("FRONTFLOW_SUPERSET_PUBLIC_URL", "").strip() or fallback


def _embed_superset_url(fallback: str) -> str:
    """The Superset URL for guest-token EMBEDS, which should differ in
    HOSTNAME from the session surfaces above.

    Superset's embedded view runs `login_user(AnonymousUserMixin(),
    force=True)` — it forcibly signs in an anonymous user to build the
    page. That rewrites the Flask session cookie, and cookies are scoped
    by host while ignoring the port, so an embed served from the same
    hostname destroys the viewer's real Superset session. In practice:
    opening a dashboard panel logs you out of Explore.

    Giving embeds their own hostname gives them their own cookie jar, so
    the anonymous session lands somewhere harmless. Locally that is
    127.0.0.1 versus localhost; in a deployment, two hostnames pointing
    at the same Superset.

    Falls back to the session URL, which restores the old behaviour —
    including the logout — so this is worth configuring.
    """
    return (
        os.environ.get("FRONTFLOW_SUPERSET_EMBED_URL", "").strip()
        or _public_superset_url(fallback)
    )


def _dashboard_names_in_form(form_id: str) -> set[str]:
    """Every dashboard name referenced by a form's compiled layout.

    This is the authorization check: a form grants access to the
    dashboards it actually displays, and nothing else.
    """
    from .. import main as main_mod  # local: avoids an import cycle

    workflow = main_mod.FORMS.get(form_id)
    if workflow is None:
        return set()

    names: set[str] = set()

    def walk(block: Optional[CompiledBlock]) -> None:
        if block is None:
            return
        if getattr(block, "type", None) == "dashboard":
            name = (block.props or {}).get("name")
            if name:
                names.add(name)
        for child in getattr(block, "children", None) or []:
            walk(child)

    for node in (workflow.all_nodes_by_id or {}).values():
        walk(getattr(node, "layout", None))
    return names



def _declared_filters(name: str) -> list[dict]:
    """Filters declared for dashboard `name`, wherever it is rendered.

    A dashboard may appear in several forms and workspaces. The union is
    taken rather than the first match, because each surface declares
    what IT needs and a filter missing from one of them is exactly the
    failure this feature exists to remove. Duplicates by name collapse:
    two surfaces asking for `Region` mean one filter.
    """
    from .. import main as main_mod  # local: avoids an import cycle

    found: dict[str, dict] = {}

    def walk(block) -> None:
        if block is None:
            return
        props = getattr(block, "props", None) or {}
        if getattr(block, "type", None) == "dashboard" and props.get("name") == name:
            for spec in props.get("declared_filters") or []:
                found.setdefault((spec.get("name") or "").strip().lower(), spec)
        for child in getattr(block, "children", None) or []:
            walk(child)

    for workflow in (main_mod.FORMS or {}).values():
        for node in (workflow.all_nodes_by_id or {}).values():
            walk(getattr(node, "layout", None))

    # Workspace panels are compiled trees of plain dicts, not blocks.
    def walk_dict(block: dict) -> None:
        props = block.get("props") or {}
        if block.get("type") == "dashboard" and props.get("name") == name:
            for spec in props.get("declared_filters") or []:
                found.setdefault((spec.get("name") or "").strip().lower(), spec)
        for child in block.get("children") or []:
            walk_dict(child)

    for layout in (getattr(main_mod, "WORKSPACE_LAYOUTS", {}) or {}).values():
        walk_dict(layout.get("layout") or {})

    return list(found.values())

def _require_dashboard_in_form(form_id: str, name: str) -> None:
    if name not in _dashboard_names_in_form(form_id):
        # 404 rather than 403: whether a dashboard exists is not
        # something an unrelated form should be able to probe.
        raise HTTPException(
            status_code=404,
            detail=f"form {form_id!r} has no dashboard named {name!r}",
        )



def _dashboard_filters(binding: dict) -> list[dict]:
    """The dashboard's native filters, as `{id, name, column, is_time}`.

    A `SetFilters` directive names filters the way an author named them
    in Superset; Superset's data mask is keyed by filter id and needs
    the target column. This is where one becomes the other.

    Degrades to an empty list rather than failing the embed: a dashboard
    that renders without filter metadata is far better than one that
    does not render. The block shows a directive it cannot place as
    unapplied instead of silently doing nothing.
    """
    dashboard_id = binding.get("superset_dashboard_id")
    if not dashboard_id:
        return []
    try:
        with SupersetClient(binding.get("connection_name")) as client:
            return client.list_native_filters(str(dashboard_id))
    except Exception:  # noqa: BLE001 - Superset unreachable or unhappy
        return []

def register(
    api: APIRouter,
    *,
    require_form_visibility,
    require_admin,
    require_workspace_visibility=None,
) -> None:
    """Attach the routes to frontflow's API router.

    Auth dependencies are injected rather than imported so this module
    does not import `main`, which imports it.
    """

    # -- form-scoped: what a dashboard block calls ------------------------

    @api.get(
        "/forms/{form_id}/dashboards/{name}/embed",
        dependencies=[Depends(require_form_visibility)],
    )
    def dashboard_embed_config(form_id: str, name: str) -> dict[str, Any]:
        """Everything the embedded SDK needs, resolving the name on first
        use so a referenced dashboard works against an empty Superset."""
        _require_dashboard_in_form(form_id, name)

        binding = provisioning.resolve_dashboard(name)
        if binding is None:
            raise HTTPException(
                status_code=503,
                detail=f"dashboard {name!r} could not be provisioned",
            )

        # Declared filters are created here, where the dashboard is
        # already being resolved — so one appears the first time the
        # dashboard is opened, like the dashboard itself.
        provisioning.ensure_declared_filters(binding, _declared_filters(name))

        connection = store.get_connection(binding["connection_name"])
        base_url = (connection or {}).get("base_url", "")

        return {
            "name": binding["name"],
            # The SDK mounts the embed from here; separate host so its
            # anonymous session cannot clobber the real one.
            "superset_domain": _embed_superset_url(base_url),
            # Session surfaces (editor, new chart) use this one.
            "superset_session_domain": _public_superset_url(base_url),
            "embed_uuid": binding["embed_uuid"],
            # Every native filter on the dashboard, so the browser can
            # resolve the NAMES a SetFilters directive carries to the
            # ids Superset's data mask needs. Resolving here rather than
            # in the chain is what keeps that operator fire-and-forget:
            # it never has to reach Superset itself.
            "filters": _dashboard_filters(binding),
            # The native filter RefreshDashboard drives. Null means the
            # dashboard renders but will not update in place; the block
            # surfaces that rather than failing silently.
            "filter_id": binding["filter_id"],
            # The numeric id, for linking to Superset's own editor.
            "superset_dashboard_id": binding["superset_dashboard_id"],
        }

    @api.post("/forms/{form_id}/dashboards/{name}/guest-token")
    def dashboard_guest_token(
        form_id: str,
        name: str,
        _: None = Depends(require_form_visibility),
        frontflow_session: str | None = Cookie(default=None),
    ) -> dict[str, str]:
        """Mint a short-lived guest token for one embedded dashboard.

        The SDK calls this repeatedly — guest tokens live about five
        minutes — so it stays cheap: the Superset access token is cached
        process-wide across calls.
        """
        _require_dashboard_in_form(form_id, name)

        # Resolve (provisioning on first use) rather than requiring that
        # /embed was called first: the embedded SDK re-invokes the token
        # callback on its own schedule, so the two calls must not depend
        # on ordering.
        binding = provisioning.resolve_dashboard(name)
        if binding is None or not binding["embed_uuid"]:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"dashboard {name!r} has no embed configuration yet — "
                    "an admin can repair it from the dashboards admin API"
                ),
            )

        try:
            with SupersetClient(binding["connection_name"]) as client:
                return {
                    "token": client.guest_token(
                        binding["embed_uuid"],
                        rls=rls.rules_for(
                            name, auth.resolve_session(frontflow_session)
                        ),
                    )
                }
        except Exception as exc:  # noqa: BLE001 - translated below
            raise _translate(exc) from exc

    # -- workspace-scoped: a dashboard panel in a workspace ----------------
    #
    # Same two gates as the form-scoped pair, with the workspace supplying
    # the ACL. A dashboard inside a form borrows that form's access; a
    # dashboard in a workspace has none to borrow, so the workspace's own
    # visibility decides — which is what makes a standalone dashboard
    # panel authorizable at all.

    if require_workspace_visibility is not None:

        def _require_dashboard_in_workspace(
            workspace_id: str, name: str
        ) -> None:
            from .. import main as main_mod  # local: avoids an import cycle

            layout = main_mod.WORKSPACE_LAYOUTS.get(workspace_id)
            names: set[str] = set()

            def walk(block: dict) -> None:
                if block.get("type") == "dashboard":
                    dashboard_name = (block.get("props") or {}).get("name")
                    if dashboard_name:
                        names.add(dashboard_name)
                for child in block.get("children") or []:
                    walk(child)

            if layout:
                walk(layout["layout"])

            if name not in names:
                # 404, not 403: whether a dashboard exists is not something
                # an unrelated workspace should be able to probe.
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"workspace {workspace_id!r} has no dashboard "
                        f"named {name!r}"
                    ),
                )

        @api.get(
            "/workspaces/{workspace_id}/dashboards/{name}/embed",
            dependencies=[Depends(require_workspace_visibility)],
        )
        def workspace_dashboard_embed(
            workspace_id: str, name: str
        ) -> dict[str, Any]:
            _require_dashboard_in_workspace(workspace_id, name)

            binding = provisioning.resolve_dashboard(name)
            if binding is None:
                raise HTTPException(
                    status_code=503,
                    detail=f"dashboard {name!r} could not be provisioned",
                )

            provisioning.ensure_declared_filters(
                binding, _declared_filters(name)
            )

            connection = store.get_connection(binding["connection_name"])
            base_url = (connection or {}).get("base_url", "")
            return {
                "name": binding["name"],
                # The SDK mounts the embed from here; separate host so
                # its anonymous session cannot clobber the real one.
                "superset_domain": _embed_superset_url(base_url),
                # Session surfaces (editor, new chart) use this one.
                "superset_session_domain": _public_superset_url(base_url),
                "embed_uuid": binding["embed_uuid"],
                "filters": _dashboard_filters(binding),
                "filter_id": binding["filter_id"],
                # The numeric id, for linking to Superset's own editor.
                # Distinct from embed_uuid, which is only good for the
                # read-only guest-token embed.
                "superset_dashboard_id": binding["superset_dashboard_id"],
            }

        @api.post(
            "/workspaces/{workspace_id}/dashboards/{name}/guest-token",
            dependencies=[Depends(require_workspace_visibility)],
        )
        def workspace_dashboard_guest_token(
            workspace_id: str,
            name: str,
            frontflow_session: str | None = Cookie(default=None),
        ) -> dict[str, str]:
            _require_dashboard_in_workspace(workspace_id, name)

            binding = provisioning.resolve_dashboard(name)
            if binding is None or not binding["embed_uuid"]:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"dashboard {name!r} has no embed configuration yet"
                    ),
                )

            try:
                with SupersetClient(binding["connection_name"]) as client:
                    return {
                        "token": client.guest_token(
                            binding["embed_uuid"],
                            rls=rls.rules_for(
                                name, auth.resolve_session(frontflow_session)
                            ),
                        )
                    }
            except Exception as exc:  # noqa: BLE001 - translated below
                raise _translate(exc) from exc

        @api.get(
            "/workspaces/{workspace_id}/explore",
            dependencies=[Depends(require_workspace_visibility)],
        )
        def workspace_explore_target(
            workspace_id: str, dataset: Optional[str] = None
        ) -> dict[str, Any]:
            """Where an Explore panel should point.

            Returns Superset's browser-facing origin plus the numeric id
            of `dataset`, when it can be resolved. A dataset Superset does
            not know yields a null id and the panel falls back to
            Superset's own dataset picker — better than a dead frame.

            No guest token is involved and none would help: Explore is
            unreachable with one. The panel loads under the viewer's own
            Superset session.
            """
            from ..dsl.connections import SupersetConnection

            connection_name = SupersetConnection.DEFAULT_NAME
            connection = store.get_connection(connection_name)
            base_url = (connection or {}).get("base_url", "")

            dataset_id: Optional[int] = None
            if dataset:
                try:
                    with SupersetClient(connection_name) as client:
                        dataset_id = client.find_dataset_id(dataset)
                except (SupersetError, SupersetUnreachable) as exc:
                    logger.info(
                        "Could not resolve dataset %r for explore: %s",
                        dataset,
                        exc,
                    )

            return {
                "superset_domain": _public_superset_url(base_url),
                "dataset": dataset,
                "dataset_id": dataset_id,
            }

    # -- admin: binding management ----------------------------------------

    @api.get("/dashboards", dependencies=[Depends(require_admin)])
    def list_dashboard_bindings() -> list[dict[str, Any]]:
        """Every known binding, with enough detail to spot a broken one."""
        return [
            {
                **binding,
                "healthy": bool(binding["embed_uuid"] and binding["filter_id"]),
            }
            for binding in store.list_dashboard_bindings()
        ]

    @api.post("/dashboards/{name}/repair", dependencies=[Depends(require_admin)])
    def repair_dashboard_binding(name: str) -> dict[str, Any]:
        """Fill in whatever a binding is missing.

        Recovers a binding created while Superset was unreachable — the
        case provisioning deliberately degrades into rather than failing
        a form render.
        """
        try:
            return provisioning.repair_dashboard(name)
        except Exception as exc:  # noqa: BLE001 - translated below
            raise _translate(exc) from exc

    @api.get("/superset/status", dependencies=[Depends(require_admin)])
    def superset_status(connection: Optional[str] = None) -> dict[str, Any]:
        """Reachability and whether the service account authenticates.

        Never raises: an unconfigured or unreachable Superset is a normal
        state that the admin UI needs to render, not an error.
        """
        try:
            with SupersetClient(connection) as client:
                return {"url": client.base_url, "detail": None, **client.ping()}
        except SupersetUnreachable as exc:
            return {
                "reachable": False,
                "authenticated": False,
                "username": None,
                "url": None,
                "detail": f"Could not reach Superset: {exc}",
            }
        except SupersetError as exc:
            return {
                "reachable": True,
                "authenticated": False,
                "username": None,
                "url": None,
                "detail": str(exc),
            }
