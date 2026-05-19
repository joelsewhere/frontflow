"""
Widget operators. Each widget corresponds to a registered widget in the
frontend (see frontend/src/components/widgets/registry.ts).

    @widgets.histogram(label="Date range", required=True, value_label="records")
    def date_range():
        return {"2025-01-06": 3, "2025-01-13": 5, ...}

    date_range = date_range()   # <- required: the call registers the widget

`@widgets.histogram` is a decorator: it produces an inert WidgetTemplate.
Calling it constructs the widget operator, which registers itself in the
current node. This mirrors @node / @backend / @displays.table —
decoration declares, calling registers. A decorated-but-never-called
widget contributes nothing.

Because the widget is also a form field referenced downstream (its
submitted value flows into a @backend function), the common pattern is
to rebind the name to the call result — `date_range = date_range()` —
so the operator can be passed as an argument.

The function is called at compile time (9a) to produce the histogram
data. In 9b this will be threaded through XCom from upstream Airflow
tasks.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .core import Operator


class HistogramWidget(Operator):
    """Distribution-filter widget — renders our existing histogram +
    selection-overlay widget in the frontend.

    The function returns a `{x_value: count}` dict. The x-axis keys may
    be ISO date strings *or* numbers (ints/floats) — the widget infers
    the axis type from the keys; it is a generic range filter, not a
    date-only one. The value produced when the user submits the form is
    `{start, end}` — the keys at the selection bounds.

    Constructed (and registered) by calling a WidgetTemplate.
    """

    kind = "widget_histogram"

    def __init__(
        self,
        func: Callable[..., Any],
        label: Optional[str] = None,
        required: bool = False,
        value_label: str = "value",
    ) -> None:
        super().__init__(id=func.__name__)
        self.func = func
        self.label = label or func.__name__.replace("_", " ").title()
        self.required = required
        self.value_label = value_label


class WidgetTemplate:
    """Result of the @widgets.histogram decorator. Inert until called;
    calling it constructs + registers a HistogramWidget operator."""

    def __init__(
        self,
        func: Callable[..., Any],
        label: Optional[str] = None,
        required: bool = False,
        value_label: str = "value",
    ) -> None:
        self.func = func
        self.label = label
        self.required = required
        self.value_label = value_label
        self.id = func.__name__

    def __call__(self) -> HistogramWidget:
        return HistogramWidget(
            self.func,
            label=self.label,
            required=self.required,
            value_label=self.value_label,
        )

    def __repr__(self) -> str:
        return f"<WidgetTemplate {self.id!r}>"


def histogram(
    *,
    label: Optional[str] = None,
    required: bool = False,
    value_label: str = "value",
) -> Callable[[Callable[..., Any]], WidgetTemplate]:
    """Decorator for the histogram widget.

    Always called with parens — even with no args — for consistency
    with @backend.branch and other parameterized decorators. Produces
    an inert WidgetTemplate; call it to add the widget to the node.
    """

    def decorator(func: Callable[..., Any]) -> WidgetTemplate:
        return WidgetTemplate(
            func, label=label, required=required, value_label=value_label
        )

    return decorator
