"""
Jinja2 setup for resolving template expressions in operator arguments.

Templates can appear in any field a user writes — most commonly
AirflowStatus.run_id, where the value depends on what a prior @backend
function returned, and Workflow.submission_id_template, which produces
the URL-facing id for a submission from the initial form values.

The lookup namespace is:

    {{ steps.<node_id>.<input_id_or_function_name> }}

`steps.foo.bar` resolves in this order:
  1. backend_returns[foo][bar]      — value returned by @backend "bar"
                                       in node "foo"
  2. form_values[foo][bar]          — submitted form value for input
                                       "bar" in node "foo"

Built-in filters and globals (in addition to Jinja's defaults):
  - `| slugify` — ASCII-only, lowercase, alphanumeric+`-`. Use for
    URL-safe ids built from user input.
  - `| timestamp_ms` — datetime → milliseconds-since-epoch string.
  - `now()` — current UTC datetime.

Returns the resolved string. Falls through gracefully (returns empty
string) for missing references — the runtime checks for "did this
resolve to a meaningful value?" before using it.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

from jinja2 import Environment, StrictUndefined, Undefined


# --- Filters / globals -----------------------------------------------------


def slugify(value: Any) -> str:
    """Reduce a string to URL-safe lowercase: ASCII alphanumerics +
    `-` only. Punctuation and whitespace become `-`; leading/trailing
    `-` stripped; runs of `-` collapsed to one.

    Designed for submission_id templates: "1428 Bayview Ave." →
    "1428-bayview-ave".
    """
    if value is None:
        return ""
    s = str(value)
    # NFKD normalization separates accented chars into base + combining
    # marks; encoding to ASCII drops the marks.
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def timestamp_ms(value: Any) -> str:
    """Datetime → milliseconds-since-epoch string. Convenient for
    composing unique ids: `"{{ ... | slugify }}-{{ now() | timestamp_ms }}"`.
    """
    if not isinstance(value, datetime):
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return str(int(value.timestamp() * 1000))


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- Environments ----------------------------------------------------------


class _SilentUndefined(Undefined):
    """Behaves like StrictUndefined for diagnostics but silently renders
    to "" when stringified. Used when templates are evaluated speculatively
    (e.g., before all upstream values are available).
    """

    def __str__(self) -> str:
        return ""


def _build_env(undefined_cls: type[Undefined]) -> Environment:
    env = Environment(
        autoescape=False,
        undefined=undefined_cls,
        # Templates here are short identifiers, not HTML — no escaping.
    )
    env.filters["slugify"] = slugify
    env.filters["timestamp_ms"] = timestamp_ms
    env.globals["now"] = _now
    return env


_env = _build_env(_SilentUndefined)
_strict_env = _build_env(StrictUndefined)


class StepLookup:
    """Indexable proxy for `steps.<ng>.<name>` access in templates."""

    def __init__(self, data: dict[str, dict[str, Any]]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        if key not in self._data:
            return _empty_step
        return _StepEntry(self._data[key])

    def __getattr__(self, key: str) -> Any:
        return self.__getitem__(key)


class _StepEntry:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data.get(key, "")

    def __getattr__(self, key: str) -> Any:
        return self._data.get(key, "")


_empty_step = _StepEntry({})


class VariablesLookup:
    """Indexable proxy for `variables.<name>` access in templates.

    Backed by a name→value dict snapshot taken once at the start of
    a render pass. Missing keys render as empty string in non-strict
    mode — matching the behavior of missing `steps.x.y` references."""

    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> str:
        return self._data.get(key, "")

    def __getattr__(self, key: str) -> str:
        return self._data.get(key, "")


def render(
    template_str: str,
    steps: dict[str, dict[str, Any]],
    *,
    strict: bool = False,
    variables: dict[str, str] | None = None,
) -> str:
    """Render a template string with `steps.<ng>.<name>` lookups and
    `variables.<name>` lookups.

    `steps` is a dict of node_id → dict of name → value, where the
    inner dict merges form values and backend returns for that
    node. `variables` is an optional name→value dict of install-
    scoped configuration values; if None, an empty namespace is
    used and any `variables.x` reference resolves to empty string.

    If `strict=True`, missing references raise; otherwise they render
    as empty string.
    """
    env = _strict_env if strict else _env
    tmpl = env.from_string(template_str)
    return tmpl.render(
        steps=StepLookup(steps),
        variables=VariablesLookup(variables or {}),
    )
