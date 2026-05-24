"""
Display blocks — the layout palette.

Every display object is a pure value object: constructing one has no
side effect. They compose into the tree a node body returns. The
backend ships the compiled tree as structured data; the frontend
renders it with themed React components (Dash-style: a component tree
as JSON, not HTML over the wire).

Containers take their children positionally and their own options as
keyword args:

    displays.Column(
        displays.Markdown("## Overview\n\nSome **text**…"),
        displays.Row(field_a, field_b),
        displays.Card(field_c, title="Details"),
    )

Content leaves:
  - Markdown(source)        prose; the frontend renders it (react-markdown)
  - Divider()               a horizontal rule
  - Image(src, alt, caption)
  - Table (@displays.table) a read-only key/value table

Containers:
  - Column(*children)              vertical stack
  - Row(*children)                 horizontal layout
  - Card(*children, title=)        bordered/elevated group
  - Section(*children, title=)     titled region
  - Callout(*children, variant=)   attention box (info/warning/success/error)

Inputs (the `inputs` module) and Buttons are also placed in this tree —
they're layout elements too. The container classes accept any operator
as a child.

Conditional layout:
  - When(condition, *children)  children render only when the condition
                                holds (see conditions.py)
  - @displays.branch            decorator — `if`/`elif`/`else` over a
                                controlling field, compiles to When
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .conditions import When, branch
from .core import Container, Operator

__all__ = [
    "Markdown",
    "Divider",
    "Image",
    "Figure",
    "KPI",
    "S3Download",
    "Table",
    "table",
    "Column",
    "Row",
    "Card",
    "Section",
    "Callout",
    "When",
    "branch",
]


# --- Content leaves --------------------------------------------------------


class Markdown(Operator):
    """A markdown prose block. The source string is shipped as-is; the
    frontend renders it (themed). The workhorse for verbose content."""

    kind = "markdown"

    def __init__(self, source: str) -> None:
        super().__init__()
        self.source = source


class Divider(Operator):
    """A horizontal rule / visual separator."""

    kind = "divider"

    def __init__(self) -> None:
        super().__init__()


class Image(Operator):
    """An image block."""

    kind = "image"

    def __init__(
        self, src: str, *, alt: str = "", caption: str = ""
    ) -> None:
        super().__init__()
        self.src = src
        self.alt = alt
        self.caption = caption


class Figure(Operator):
    """An image rendered from bytes a `@backend` returned.

    The backend produces raw image bytes — typically a matplotlib
    figure saved to PNG or SVG — and `Figure` displays them. The
    runtime stores the bytes in a content-addressed blob store
    scoped to the submission; the block carries a small handle dict,
    and the frontend resolves it to a proxy URL that streams the
    bytes with the right Content-Type.

    `data` is a `StepRef` pointing at a node-internal `@backend` that
    returned `bytes` — e.g. `steps.<node>.<fn_name>`.

    `alt` is the image's alt text. `caption` renders below the image.
    `width` and `height` (CSS dimension strings — "200px", "100%",
    "20rem") override the default fill-width-auto-height sizing.

        @backend
        def chart(steps):
            import matplotlib.pyplot as plt, io
            fig, ax = plt.subplots()
            ax.bar(steps.previous.codes, steps.previous.totals)
            buf = io.BytesIO()
            fig.savefig(buf, format='png', bbox_inches='tight')
            return buf.getvalue()

        displays.Figure(
            data=steps.review.chart,
            caption="Revenue by charge code",
            alt="Bar chart of revenue per charge code",
        )
    """

    kind = "figure"

    def __init__(
        self,
        *,
        data,  # StepRef pointing at a bytes-returning @backend
        alt: str = "",
        caption: str = "",
        width: Optional[str] = None,
        height: Optional[str] = None,
    ) -> None:
        # Deferred import — references imports core which displays
        # transitively depends on at module load time.
        from .references import StepRef
        super().__init__()
        if not isinstance(data, StepRef):
            raise TypeError(
                "Figure.data must be a StepRef (steps.<node>.<fn>); "
                f"got {type(data).__name__}. The referenced @backend "
                "should return raw image bytes (e.g. fig.savefig to "
                "a BytesIO buffer)."
            )
        self.data = data
        self.alt = alt
        self.caption = caption
        self.width = width
        self.height = height


class KPI(Operator):
    """A single labeled metric — a label above a large centered value.

        displays.KPI(label="Total revenue", value="$1,234,567")
        displays.KPI(label="Records", value=2847)

    `value` may be a string or a number (rendered as a string). Both
    `label` and `value` are templated — `value="{{ steps.x.totals }}"`
    works, so an analysis backend can return a dict and the layout
    interpolates the numbers in.

    Drop two of these in a Row for a two-column metric strip; drop
    more for a wider strip. The block itself fills the width its
    container gives it.
    """

    kind = "kpi"

    def __init__(self, *, label: str, value) -> None:
        super().__init__()
        self.label = label
        # value comes through as a string at render — numbers stringify
        # naturally; templates resolve client-side.
        self.value = str(value)


class S3Download(Operator):
    """A download link for an object stored in S3.

    The block carries `bucket` and `key` (key is templated against the
    form's `steps` namespace). When the user clicks the link, the form
    server resolves a fresh presigned URL via `S3Hook` and 302-redirects
    the browser to S3 — so the link always works, regardless of how
    long the page has been open.

    `connection` names an entry in the connection store (defaults to
    `aws_default`); falls through to boto3's default credential chain
    when missing, same semantics as `S3File`.

    `expires_in` is the TTL of the *click-through* presigned URL — short
    by design (seconds). The default is 300s, plenty for the browser's
    redirect to complete. Authors don't need to tune this for "how long
    the link is valid" — the proxy generates a fresh URL on every
    click. Use a longer value only if your network path between the
    redirect and the S3 GET is unusually slow.

    `label` is the visible link text. Defaults to a generic "Download".
    """

    kind = "s3_download"

    def __init__(
        self,
        *,
        bucket: str,
        key: str,
        label: str = "Download",
        connection: Optional[str] = None,
        expires_in: int = 300,
        id: Optional[str] = None,
    ) -> None:
        super().__init__(id=id or _slug_for_key(key))
        self.bucket = bucket
        self.key = key
        self.label = label
        self.connection = connection
        self.expires_in = expires_in


def _slug_for_key(key: str) -> str:
    """A stable, URL-safe id derived from a templated S3 key. Used as
    the default `id` on an S3Download — multiple downloads in one node
    that share the same key are an author error and would collide here,
    in which case the author should pass `id=` explicitly."""
    import re as _re
    # Strip template tokens, replace path separators with `_`, collapse
    # non-alphanum runs, trim. Best-effort readability for the URL.
    cleaned = _re.sub(r"\{\{[^}]*\}\}", "", key)
    cleaned = cleaned.replace("/", "_")
    cleaned = _re.sub(r"[^A-Za-z0-9_]+", "_", cleaned).strip("_")
    return cleaned or "download"


class Table(Operator):
    """A read-only key/value table.

    Constructed by *calling* a TableTemplate (see @displays.table). The
    decorated function returns a dict, invoked at compile time.
    """

    kind = "table"

    def __init__(
        self, func: Callable[..., Any], title: Optional[str] = None
    ) -> None:
        super().__init__(id=func.__name__)
        self.func = func
        self.title = title


class TableTemplate:
    """Result of @displays.table. Inert until called; calling it
    produces a Table block (a pure value) to place in the layout tree."""

    def __init__(
        self, func: Callable[..., Any], title: Optional[str] = None
    ) -> None:
        self.func = func
        self.title = title
        self.id = func.__name__

    def __call__(self) -> Table:
        return Table(self.func, title=self.title)

    def __repr__(self) -> str:
        return f"<TableTemplate {self.id!r}>"


def table(
    arg: Any = None, /, *, title: Optional[str] = None
) -> Any:
    """Decorator for table blocks. Supports `@displays.table` and
    `@displays.table(title="…")`. Either way it produces a TableTemplate
    — call it to get a Table block for the layout tree."""
    if callable(arg):
        return TableTemplate(arg, title=title)

    def decorator(func: Callable[..., Any]) -> TableTemplate:
        return TableTemplate(func, title=title or arg)

    return decorator


# --- Containers ------------------------------------------------------------


class Column(Container):
    """Vertical stack of child blocks. The common root container."""

    kind = "column"


class Row(Container):
    """Horizontal layout — children sit side by side."""

    kind = "row"


class Card(Container):
    """A bordered / elevated group, optionally titled."""

    kind = "card"

    def __init__(self, *children: Operator, title: Optional[str] = None) -> None:
        super().__init__(*children)
        self.title = title


class Section(Container):
    """A titled region — a heading plus its content."""

    kind = "section"

    def __init__(self, *children: Operator, title: Optional[str] = None) -> None:
        super().__init__(*children)
        self.title = title


class Callout(Container):
    """An attention box. `variant` picks the themed treatment:
    info | warning | success | error."""

    kind = "callout"

    _VARIANTS = ("info", "warning", "success", "error")

    def __init__(self, *children: Operator, variant: str = "info") -> None:
        super().__init__(*children)
        if variant not in self._VARIANTS:
            raise ValueError(
                f"Callout variant must be one of {self._VARIANTS}, "
                f"got {variant!r}."
            )
        self.variant = variant
