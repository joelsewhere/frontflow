"""Reusable helpers for form authors.

These are convenience utilities — forms can use them or roll their
own logic. Keep them small, focused, and format-agnostic. Anything
form-specific (date arithmetic, bucket conventions, column
semantics) belongs in the form file, not here.
"""

from .redistribution import DROPPED, apply_redistribution_mapping

__all__ = ["DROPPED", "apply_redistribution_mapping"]
