"""
Button operator. A node's buttons are the actions the user can take.

Two kinds:

  - Submit button (the default) — submits the form. `>>` wires what
    runs after it (a @backend call, then external tasks). Multiple
    submit buttons (Approve / Request changes / Reject) all submit the
    same data but differ in which boolean arg the @backend receives.
    The branching decision lives in @backend.branch, not on the Button.

  - Link button (`url=` set) — navigates to a URL instead of
    submitting. It has no `>>` execution chain and doesn't count as one
    of the node's advancing buttons; it's purely a layout element.

`variant` selects the themed styling: primary | secondary | danger.
"""

from __future__ import annotations

from typing import Optional

from .core import Operator


class Button(Operator):
    """A button in a node — a submit action or, with `url=`, a link."""

    kind = "button"

    _VARIANTS = ("primary", "secondary", "danger")

    def __init__(
        self,
        label: str = "Submit",
        *,
        id: Optional[str] = None,
        variant: str = "primary",
        url: Optional[str] = None,
        new_tab: bool = True,
    ) -> None:
        super().__init__(id=id)
        if variant not in self._VARIANTS:
            raise ValueError(
                f"Button variant must be one of {self._VARIANTS}, "
                f"got {variant!r}."
            )
        self.label = label
        self.variant = variant
        self.url = url
        self.new_tab = new_tab

    @property
    def is_link(self) -> bool:
        """A link button navigates to `url` instead of submitting."""
        return self.url is not None

    def was_clicked(self) -> "FieldCondition":
        """A condition: this button is the one the user clicked when
        submitting the node. Used to gate after-submit content with
        `displays.When` — e.g. a status callout that appears below the
        report after Approve / Deny:

            approve = Button("Approve", id="approve")
            deny    = Button("Deny", id="deny", variant="danger")
            return (
                ...report content...,
                (approve, deny),
                displays.When(
                    approve.was_clicked(),
                    displays.Callout("Approved", variant="success"),
                ),
                displays.When(
                    deny.was_clicked(),
                    displays.Callout("Rejected", variant="warning"),
                ),
            )

        Evaluates to False while the form is being filled in (no
        button has been clicked yet) and to True after submit if THIS
        button's id matches the recorded `step.button_clicked`.
        """
        # Deferred import — actions.py would otherwise pull in
        # conditions.py at import time, and conditions.py imports
        # `_normalize_layout` from core.py which depends on actions.
        from .conditions import FieldCondition

        return FieldCondition(self, "button_clicked", None)

    def __repr__(self) -> str:
        kind = "link" if self.is_link else "submit"
        return f"<Button {kind} id={self.id!r} label={self.label!r}>"
