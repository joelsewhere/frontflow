"""
Form input operators. Each subclass corresponds to one field type the
frontend knows how to render — see the block REGISTRY in BlockTree.tsx.

Usage in a node body:

    name      = inputs.Text(label="Full name", required=True)
    sqft      = inputs.Integer(placeholder="e.g. 2400")
    notes     = inputs.TextBlock(label="Notes")
    kind      = inputs.Select(options=["A", "B", "C"], required=True)
    region    = inputs.Radio(options=["North", "South"], required=True)
    tags      = inputs.MultiSelect(options=["x", "y", "z"])
    start     = inputs.Date(label="Start date", required=True)
    window    = inputs.DateRange(label="Reporting window")
    budget    = inputs.NumberRange(label="Budget range")
    coverage  = inputs.CheckboxGrid(rows=["A", "B"], columns=["Mon", "Tue"])
    agree     = inputs.Checkbox(label="I agree", required=True)

The variable name becomes the field id by default. To override, pass
`input_id=...` to the constructor.

Each subclass declares its type-specific configuration (options, date
bounds, grid rows/columns) and surfaces it through `extra_props()`,
which the compiler merges into the field block. `options` / `default`
and the grid's `rows` / `columns` are plain literals today; a later
step lets them also accept upstream references.
"""

from __future__ import annotations

from typing import Any, Optional

from .conditions import FieldCondition
from .core import Operator
from .references import StepRef


class Input(Operator):
    """Base for input operators."""

    kind = "input"
    field_type: str = ""  # overridden by subclasses

    def __init__(
        self,
        *,
        input_id: Optional[str] = None,
        label: Optional[str] = None,
        required: bool = False,
        placeholder: str = "",
        default: Any = None,
        help: str = "",
    ) -> None:
        super().__init__(id=input_id)
        self.label = label
        self.required = required
        self.placeholder = placeholder
        self.default = default
        # Optional descriptive hint shown beneath the field.
        self.help = help

    def extra_props(self) -> dict[str, Any]:
        """Type-specific props merged into the compiled field block —
        options, date bounds, grid rows/columns. The base contributes
        the help text when one is set; subclasses extend via super()."""
        props: dict[str, Any] = {}
        if self.help:
            props["help"] = self.help
        return props

    # --- Condition builders --------------------------------------------
    # Used to gate a `displays.When` on this field's value:
    #     displays.When(pet.equals("Yes"), follow_up)

    def equals(self, value: Any) -> FieldCondition:
        """A condition: this field's value equals `value`."""
        return FieldCondition(self, "equals", value)

    def not_equals(self, value: Any) -> FieldCondition:
        """A condition: this field's value does not equal `value`."""
        return FieldCondition(self, "not_equals", value)

    def in_(self, values: Any) -> FieldCondition:
        """A condition: this field's value is one of `values`."""
        return FieldCondition(self, "in", list(values))

    def not_in(self, values: Any) -> FieldCondition:
        """A condition: this field's value is none of `values`."""
        return FieldCondition(self, "not_in", list(values))

    def is_filled(self) -> FieldCondition:
        """A condition: this field has a non-empty / truthy value."""
        return FieldCondition(self, "truthy", None)

    def is_blank(self) -> FieldCondition:
        """A condition: this field is empty / falsy."""
        return FieldCondition(self, "falsy", None)


# --- Simple inputs ---------------------------------------------------------


class Text(Input):
    """Single-line text input. Value: a string."""

    field_type = "text"


class Integer(Input):
    """Numeric input. (Underlying field_type is "number"; we accept
    floats too despite the name — the runtime doesn't enforce
    integer-only.) Value: a number."""

    field_type = "number"


class TextBlock(Input):
    """Multi-line textarea. Value: a string."""

    field_type = "textarea"


class Email(Input):
    """Single-line email input. The frontend validates the address
    format and shows an email keyboard on mobile. Value: a string."""

    field_type = "email"


class Phone(Input):
    """Single-line telephone input. The frontend shows a telephone
    keypad on mobile. Value: a string (free-form — numbers are not
    reformatted, since formats vary by country)."""

    field_type = "tel"


class URL(Input):
    """Single-line URL input. The frontend validates the URL format
    and shows a URL keyboard on mobile. Value: a string."""

    field_type = "url"


class Checkbox(Input):
    """A single boolean checkbox. When `required` is set the box must be
    ticked for the form to submit (the consent pattern). Value: a
    bool."""

    field_type = "checkbox"


# --- Date inputs -----------------------------------------------------------


class Date(Input):
    """A calendar date picker. Value: an ISO date string ("YYYY-MM-DD").
    `min` / `max` bound the selectable range — pass ISO date strings."""

    field_type = "date"

    def __init__(
        self,
        *,
        input_id: Optional[str] = None,
        label: Optional[str] = None,
        required: bool = False,
        default: Any = None,
        help: str = "",
        min: Optional[str] = None,
        max: Optional[str] = None,
    ) -> None:
        super().__init__(
            input_id=input_id,
            label=label,
            required=required,
            default=default,
            help=help,
        )
        self.min = min
        self.max = max

    def extra_props(self) -> dict[str, Any]:
        props: dict[str, Any] = super().extra_props()
        if self.min is not None:
            props["min"] = self.min
        if self.max is not None:
            props["max"] = self.max
        return props


class DateRange(Input):
    """A pair of date pickers — a start and an end. Value:
    `{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}`."""

    field_type = "date_range"


class NumberRange(Input):
    """A pair of numeric inputs — a low and a high bound. Value:
    `{"min": number, "max": number}`."""

    field_type = "number_range"


class Time(Input):
    """A time-of-day picker. Value: a 24-hour time string ("HH:MM").
    `min` / `max` bound the selectable range — pass "HH:MM" strings."""

    field_type = "time"

    def __init__(
        self,
        *,
        input_id: Optional[str] = None,
        label: Optional[str] = None,
        required: bool = False,
        default: Any = None,
        help: str = "",
        min: Optional[str] = None,
        max: Optional[str] = None,
    ) -> None:
        super().__init__(
            input_id=input_id,
            label=label,
            required=required,
            default=default,
            help=help,
        )
        self.min = min
        self.max = max

    def extra_props(self) -> dict[str, Any]:
        props = super().extra_props()
        if self.min is not None:
            props["min"] = self.min
        if self.max is not None:
            props["max"] = self.max
        return props


class Rating(Input):
    """A discrete rating scale — e.g. 1–5 stars. `max` sets the top of
    the scale (default 5). Value: an integer from 1 to `max`, or null
    if unrated."""

    field_type = "rating"

    def __init__(
        self,
        *,
        input_id: Optional[str] = None,
        label: Optional[str] = None,
        required: bool = False,
        default: Any = None,
        help: str = "",
        max: int = 5,
    ) -> None:
        super().__init__(
            input_id=input_id,
            label=label,
            required=required,
            default=default,
            help=help,
        )
        if max < 2:
            raise ValueError("Rating max must be at least 2")
        self.max = max

    def extra_props(self) -> dict[str, Any]:
        props = super().extra_props()
        props["max"] = self.max
        return props


class Slider(Input):
    """A slider for picking one number on a continuum. `min` / `max`
    bound the range (default 0–100); `step` is the increment (default
    1). Value: a number."""

    field_type = "slider"

    def __init__(
        self,
        *,
        input_id: Optional[str] = None,
        label: Optional[str] = None,
        required: bool = False,
        default: Any = None,
        help: str = "",
        min: float = 0,
        max: float = 100,
        step: float = 1,
    ) -> None:
        super().__init__(
            input_id=input_id,
            label=label,
            required=required,
            default=default,
            help=help,
        )
        if max <= min:
            raise ValueError("Slider max must be greater than min")
        if step <= 0:
            raise ValueError("Slider step must be positive")
        self.min = min
        self.max = max
        self.step = step

    def extra_props(self) -> dict[str, Any]:
        props = super().extra_props()
        props["min"] = self.min
        props["max"] = self.max
        props["step"] = self.step
        return props


# --- File uploads ----------------------------------------------------------


class _FileInput(Input):
    """Shared base for the file-upload inputs.

    `max_size_mb` caps the upload (default 25 MB). `accept` is a list
    of allowed file extensions, e.g. ["pdf", "csv"]; an empty list
    accepts any type. Both limits are enforced on the server, not just
    in the browser.
    """

    DEFAULT_MAX_SIZE_MB = 25

    def __init__(
        self,
        *,
        input_id: Optional[str] = None,
        label: Optional[str] = None,
        required: bool = False,
        help: str = "",
        max_size_mb: float = DEFAULT_MAX_SIZE_MB,
        accept: Optional[list[str]] = None,
    ) -> None:
        super().__init__(
            input_id=input_id,
            label=label,
            required=required,
            help=help,
        )
        if max_size_mb <= 0:
            raise ValueError("max_size_mb must be positive")
        self.max_size_mb = max_size_mb
        # Normalise extensions — lower-case, no leading dot.
        self.accept = [
            a.lower().lstrip(".") for a in (accept or [])
        ]

    def extra_props(self) -> dict[str, Any]:
        props = super().extra_props()
        props["max_size_mb"] = self.max_size_mb
        props["accept"] = self.accept
        return props


class File(_FileInput):
    """A file upload that is NOT persisted to durable storage.

    The uploaded bytes reach the backend and are exposed to `@backend`
    functions as a file handle — `.filename`, `.content_type`,
    `.size`, and `.read()` / `.bytes` for the raw content (usable with
    `io` / `pandas`). Nothing is written to S3 or disk; the submission
    records only the filename, size, and content-type.

    Use this to process an upload during the run — parse a CSV, read a
    config — when the file itself need not be kept.
    """

    field_type = "file"


class S3File(_FileInput):
    """A file upload that is persisted to S3.

    The bytes are streamed to S3; the submission value is an S3
    reference carrying `bucket`, `key`, `filename`, `size`, and
    `content_type`. A `@backend` function receives a handle exposing
    those fields plus `.read()` (fetches the bytes back from S3) and
    `.url()` (a time-limited download link).

    `key` is the S3 object key — required, and templatable. It may
    contain `{{ steps.<node>.<field> }}` tokens (with the usual
    filters, e.g. `| slugify`) and a literal `{filename}` placeholder
    that expands to the uploaded file's name. The key resolves to an
    exact path: `key="receipts/{{ steps.intake.client | slugify }}/
    {filename}"` might become `receipts/acme-co/invoice.pdf`. There is
    no anti-collision segment — if a resolved key matches an existing
    object, the upload overwrites it. Template tokens naming an
    earlier step resolve to that step's value; tokens naming a field
    on the same screen as the upload resolve to its value at the
    moment of upload (a snapshot — later edits do not move the file).

    `bucket` overrides the bucket configured on the AWS connection.
    AWS credentials resolve from a stored `aws` connection first, then
    boto3's default chain. If S3 is unreachable the upload fails
    loudly — it is never silently dropped.
    """

    field_type = "s3file"

    def __init__(
        self,
        *,
        key: str,
        input_id: Optional[str] = None,
        label: Optional[str] = None,
        required: bool = False,
        help: str = "",
        max_size_mb: float = _FileInput.DEFAULT_MAX_SIZE_MB,
        accept: Optional[list[str]] = None,
        bucket: Optional[str] = None,
    ) -> None:
        super().__init__(
            input_id=input_id,
            label=label,
            required=required,
            help=help,
            max_size_mb=max_size_mb,
            accept=accept,
        )
        if not key or not str(key).strip():
            raise ValueError("S3File requires a non-empty key")
        self.key = key
        self.bucket = bucket

    def extra_props(self) -> dict[str, Any]:
        props = super().extra_props()
        # `key` and `bucket` are server-side upload targets — not
        # exposed to the browser; the upload endpoint reads them from
        # the compiled form's file_spec.
        return props


# --- Sankey mapping --------------------------------------------------------


class Sankey(Input):
    """A weighted many-to-many mapping between two columns of values.

    The user draws connections from column-A values to column-B
    values, each carrying a weight — visualised as a Sankey diagram
    where a ribbon's thickness tracks its weight.

    `column_a` / `column_b` give the values for each side. Each may be
    a static `list[str]`, or an `steps.<node>.<field>` reference whose
    list value is resolved at runtime (e.g. an earlier MultiSelect's
    selections, or a `@backend`-built list).

    `normalize` (default True): the weights leaving each A-value must
    sum to 100 — the percentage case ("10% to Z, 90% to Y"). When
    False, weights are free-form numbers with no sum constraint.

    Submitted value: a list of connection triples,
    `[{"from": "A1", "to": "B2", "weight": 90}, ...]`. `required`
    means at least one connection must be drawn; individual A-values
    may still be left unmapped.
    """

    field_type = "sankey"

    def __init__(
        self,
        *,
        column_a: "list[str] | StepRef",
        column_b: "list[str] | StepRef",
        input_id: Optional[str] = None,
        label: Optional[str] = None,
        required: bool = False,
        help: str = "",
        normalize: bool = True,
    ) -> None:
        super().__init__(
            input_id=input_id,
            label=label,
            required=required,
            help=help,
        )
        self.column_a = (
            column_a
            if isinstance(column_a, StepRef)
            else list(column_a)
        )
        self.column_b = (
            column_b
            if isinstance(column_b, StepRef)
            else list(column_b)
        )
        self.normalize = normalize

    def extra_props(self) -> dict[str, Any]:
        props = super().extra_props()
        props["column_a"] = self.column_a
        props["column_b"] = self.column_b
        props["normalize"] = self.normalize
        return props


# --- Option-backed inputs --------------------------------------------------


class ChoiceInput(Input):
    """Base for inputs backed by a list of `options` — Select, Radio,
    MultiSelect, CheckboxList.

    `options` is normally a list of strings, but may also be an
    `steps.<node>.<field>` reference — the choices are then resolved
    at runtime from an earlier node's value (e.g. a `MultiSelect`'s
    selections or a `@backend`'s returned list).
    """

    def __init__(
        self,
        *,
        options: "list[str] | StepRef",
        input_id: Optional[str] = None,
        label: Optional[str] = None,
        required: bool = False,
        default: Any = None,
        help: str = "",
    ) -> None:
        super().__init__(
            input_id=input_id,
            label=label,
            required=required,
            default=default,
            help=help,
        )
        # A static list is copied; an upstream reference is kept as-is
        # for the compiler to turn into a resolve-at-runtime descriptor.
        self.options = (
            options if isinstance(options, StepRef) else list(options)
        )

    def extra_props(self) -> dict[str, Any]:
        props = super().extra_props()
        props["options"] = self.options
        return props


class Select(ChoiceInput):
    """Dropdown with a fixed list of options. Value: one option
    string."""

    field_type = "select"


class Radio(ChoiceInput):
    """A radio-button group — one choice from a fixed list, every option
    visible at once. Value: one option string."""

    field_type = "radio"


class MultiSelect(ChoiceInput):
    """A search-as-you-type dropdown for picking several options.
    Value: a list of option strings."""

    field_type = "multi_select"


class CheckboxList(ChoiceInput):
    """A flat list of options, each with its own checkbox, laid out as
    a grid — every option visible at once. Value: a list of the checked
    option strings (the same shape as MultiSelect). `columns` fixes the
    grid column count; leave it None for a responsive layout."""

    field_type = "checkbox_list"

    def __init__(
        self,
        *,
        options: "list[str] | StepRef",
        input_id: Optional[str] = None,
        label: Optional[str] = None,
        required: bool = False,
        default: Any = None,
        help: str = "",
        columns: Optional[int] = None,
    ) -> None:
        super().__init__(
            options=options,
            input_id=input_id,
            label=label,
            required=required,
            default=default,
            help=help,
        )
        self.columns = columns

    def extra_props(self) -> dict[str, Any]:
        props = super().extra_props()
        if self.columns is not None:
            props["columns"] = self.columns
        return props


# --- Matrix input ----------------------------------------------------------


class CheckboxGrid(Input):
    """A matrix of checkboxes — one row per `rows` entry, one column per
    `columns` entry, every cell independently checkable. Value: each row
    mapped to the list of its checked columns,
    `{"row a": ["col 1", "col 3"], ...}`."""

    field_type = "checkbox_grid"

    def __init__(
        self,
        *,
        rows: list[str],
        columns: list[str],
        input_id: Optional[str] = None,
        label: Optional[str] = None,
        required: bool = False,
        default: Any = None,
        help: str = "",
    ) -> None:
        super().__init__(
            input_id=input_id,
            label=label,
            required=required,
            default=default,
            help=help,
        )
        self.rows = list(rows)
        self.columns = list(columns)

    def extra_props(self) -> dict[str, Any]:
        props = super().extra_props()
        props["rows"] = self.rows
        props["columns"] = self.columns
        return props
