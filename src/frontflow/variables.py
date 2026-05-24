"""
Workflow-time access to install-scoped variables.

Variables hold non-secret install configuration — a bucket name, a
default region, a webhook URL — that workflow authors want to
reference without hard-coding. Two access surfaces:

  1. **Template-resolved at runtime**:
        TriggerDag(
            dag_id="{{ variables.daily_export_dag }}",
            ...,
        )
     Resolved by the existing template engine alongside `steps.x.y`.
     Missing reference resolves to empty string (non-strict mode).

  2. **Python helper, evaluated at workflow load**:
        from frontflow import variables
        REGION = variables.get("default_region")
     Use this for boot-time constants — DAG ids selected by region,
     conditionals on environment, etc. A missing variable raises
     `MissingVariableError` so the failure surfaces immediately
     during the workflow scan, not later when a step runs.

The two surfaces share the same underlying store and encryption.
Both go through `frontflow.dsl.store.get_variable` so a value
written via the admin UI is visible to both paths.

Variables are stored encrypted at rest as defense-in-depth. The
intended use is non-secret, but users will inevitably put secrets
here — encrypting protects those who misuse. For first-class
credential management (with operator-side auth wiring) use
Connections instead.
"""

from __future__ import annotations

from typing import Optional

from .dsl import store


class MissingVariableError(KeyError):
    """Raised by `variables.get(name)` when no variable with that
    name is stored. Workflow authors see this at workflow-load
    time, surfaced in `/api/forms` as a LOAD_ERROR — so a missing
    variable fails fast and visibly rather than silently producing
    empty strings deep in operator config.

    For the more permissive "resolve missing as empty string"
    behavior, use the `{{ variables.x }}` template syntax instead —
    that path matches the existing `steps.x.y` non-strict semantics.
    """


def get(name: str, default: Optional[str] = None) -> str:
    """Return the value of variable `name`.

    Raises `MissingVariableError` if no such variable exists and no
    `default` was given. Pass a `default` to opt into permissive
    behavior — useful for optional configuration ("if the user
    hasn't set this, fall back to <value>").
    """
    value = store.get_variable(name)
    if value is not None:
        return value
    if default is not None:
        return default
    raise MissingVariableError(
        f"no variable named {name!r} is stored — add one in the "
        "Variables admin page, or pass a default to variables.get()."
    )
