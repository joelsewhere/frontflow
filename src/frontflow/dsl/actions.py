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

    def __repr__(self) -> str:
        kind = "link" if self.is_link else "submit"
        return f"<Button {kind} id={self.id!r} label={self.label!r}>"
