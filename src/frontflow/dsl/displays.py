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
