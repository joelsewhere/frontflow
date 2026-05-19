"""
Hierarchical status taxonomy.

A status is a class; refining a status into a more specific one is
subclassing it. Ancestry questions therefore use the language's own
machinery — `issubclass(status, Affected)` — rather than a hand-rolled
tree walk, and nesting to any depth is just further subclassing.

The value a workflow step carries is a status *class* — the class
object itself, not an instance. The step-status taxonomy:

    StepStatus              abstract base — a marker, never assigned
      Unaffected            nothing this step reads changed
      Affected              something it depends on changed
        NeedsReview         only a display dependency changed — the
                            data is valid, just possibly stale
        NeedsInput          a functional dependency changed — the data
                            may be invalid; the step must be re-submitted

Declare sibling states in increasing order of severity: that order is
what `StepStatus.strictest` uses to break ties when a step is hit by
more than one change.
"""

from __future__ import annotations

import re
from typing import Iterable, Iterator


def _snake(camel: str) -> str:
    """`NeedsInput` -> `needs_input` — the serialized key form."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", camel).lower()


class StepStatus:
    """Abstract root of the step-status taxonomy.

    Every state is a subclass; a sub-state subclasses its parent state,
    to any depth. `StepStatus` itself is a marker — never assigned to a
    step.

    A status *value* is a subclass object, e.g. `NeedsInput`. Test
    ancestry with the builtin — `issubclass(status, Affected)` is true
    for `Affected` and every state beneath it, at any depth, so call
    sites never hard-code a level. Crossing the storage / API boundary
    goes through `.key` and `parse()`.
    """

    # The taxonomy registry — every declared state by its key, in
    # declaration order. Filled by __init_subclass__.
    _by_key: dict[str, type["StepStatus"]] = {}

    # Set on each subclass by __init_subclass__:
    key: str = ""               # serialized leaf key, e.g. "needs_input"
    path: tuple[str, ...] = ()   # ("affected", "needs_input")
    depth: int = 0               # 1 for a top-level state, 2 for a child
    _severity: int = -1          # declaration order — the tie-break rank

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if len(cls.__bases__) != 1:
            raise TypeError(
                f"a status refines exactly one parent status; "
                f"{cls.__name__} has bases {cls.__bases__}"
            )
        cls.key = _snake(cls.__name__)
        parent = cls.__bases__[0]
        cls.path = (
            (cls.key,)
            if parent is StepStatus
            else parent.path + (cls.key,)
        )
        cls.depth = len(cls.path)
        if cls.key in StepStatus._by_key:
            raise ValueError(f"duplicate status key {cls.key!r}")
        cls._severity = len(StepStatus._by_key)
        StepStatus._by_key[cls.key] = cls

    def __init__(self) -> None:
        # A status is the class itself; instances are never used.
        raise TypeError(
            f"{type(self).__name__} is a status class — use it directly, "
            f"not {type(self).__name__}()"
        )

    @classmethod
    def all(cls) -> Iterator[type["StepStatus"]]:
        """Every declared state at or below `cls`, in declaration
        order. `StepStatus.all()` walks the whole taxonomy."""
        return (s for s in StepStatus._by_key.values() if issubclass(s, cls))

    @staticmethod
    def parse(key: str) -> type["StepStatus"]:
        """The status class for a serialized key — the leaf key
        (`needs_input`) or a full dotted path (`affected.needs_input`).
        Raises ValueError on an unknown status."""
        leaf = key.rsplit(".", 1)[-1]
        try:
            return StepStatus._by_key[leaf]
        except KeyError:
            raise ValueError(f"unknown status {key!r}") from None

    @staticmethod
    def strictest(
        statuses: Iterable[type["StepStatus"]],
    ) -> type["StepStatus"]:
        """The most severe of `statuses` — the winner when a step is
        assigned more than one at once. Severity is sibling declaration
        order. Raises ValueError on an empty iterable."""
        return max(statuses, key=lambda s: s._severity)


class Unaffected(StepStatus):
    """Nothing this step reads changed — its data stands as submitted."""


class Affected(StepStatus):
    """Something this step depends on changed. The sub-states say how."""


class NeedsReview(Affected):
    """Only a display dependency changed — a `default` seed or a
    `{{ template }}`. The submitted data is still valid, just possibly
    stale; the user should glance at it, not necessarily refill it."""


class NeedsInput(Affected):
    """A functional dependency changed — an `options` set or a
    condition. The submitted data may no longer be valid; the step
    must be re-submitted."""
