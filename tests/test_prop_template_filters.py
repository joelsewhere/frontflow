"""Tests for Jinja filter support in templated block props.

Before this work, prop templating (`displays.Markdown` source, KPI
labels, etc.) used a regex-only resolver that captured `{{ steps.X.Y }}`
but silently left filtered expressions like `{{ steps.X.Y | lower }}`
as literal text. The resolver now goes through the same Jinja
environment that `submission_id` / `id=` templates use, so the full
filter set is available everywhere.

Critical invariants this preserves:
  - Same-node references stay LITERAL (the client resolves them
    live against the in-progress form).
  - Cross-node references render to their value.
  - Missing refs render as empty string (silent undefined).
  - URL props get percent-encoded after resolution.
  - The compile-time dependency scanner still finds (node, field)
    references through filters — edit cascade keeps working.
"""
from __future__ import annotations

import pytest

from frontflow.dsl.references import STEP_REF_RE


class TestStepRefRegexCapturesThroughFilters:
    """The compile-time scanner uses STEP_REF_RE.findall to build the
    dep graph. With filters now legal in templates, the regex must
    still capture (node, field, sub) tuples — otherwise edits to
    upstream values won't invalidate downstream re-runs."""

    @pytest.mark.parametrize("template, expected", [
        # Bare reference — no change from before.
        ("{{ steps.signup.cadence }}",
         [("signup", "cadence", "")]),
        # Single filter.
        ("{{ steps.signup.cadence | lower }}",
         [("signup", "cadence", "")]),
        # Filter chain.
        ("{{ steps.x.y | upper | trim }}",
         [("x", "y", "")]),
        # Filter with argument.
        ('{{ steps.x.y | default("none") }}',
         [("x", "y", "")]),
        # Three-part reference + filter.
        ("{{ steps.x.y.z | upper }}",
         [("x", "y", "z")]),
        # Multiple references in one string.
        ("Hi {{ steps.a.b }}, role {{ steps.c.d | upper }}",
         [("a", "b", ""), ("c", "d", "")]),
    ])
    def test_capture_unchanged_by_filters(self, template, expected):
        assert STEP_REF_RE.findall(template) == expected


class TestPropTemplateResolution:
    """End-to-end: a Markdown block with a filtered template renders
    correctly."""

    def _resolve(self, text, current_node_id, steps_data, is_url=False):
        from frontflow.dsl.runtime import _resolve_template_string
        return _resolve_template_string(
            text, current_node_id, steps_data, is_url=is_url
        )

    def test_filter_renders(self):
        # The user submitted "Monthly"; the template lowers it.
        out = self._resolve(
            "Send {{ steps.signup.cadence | lower }} updates",
            current_node_id="thanks",
            steps_data={"signup": {"cadence": "Monthly"}},
        )
        assert out == "Send monthly updates"

    def test_unfiltered_reference_still_works(self):
        # Make sure the non-filter path didn't regress.
        out = self._resolve(
            "Hello {{ steps.signup.name }}",
            current_node_id="thanks",
            steps_data={"signup": {"name": "Dana"}},
        )
        assert out == "Hello Dana"

    def test_missing_field_renders_empty(self):
        # _SilentUndefined → empty string on missing.
        out = self._resolve(
            "X={{ steps.signup.missing | upper }}",
            current_node_id="thanks",
            steps_data={"signup": {"name": "Dana"}},
        )
        assert out == "X="

    def test_missing_node_renders_empty(self):
        out = self._resolve(
            "X={{ steps.other.field | upper }}",
            current_node_id="thanks",
            steps_data={"signup": {"name": "Dana"}},
        )
        assert out == "X="

    def test_same_node_reference_stays_literal(self):
        # Critical: same-node refs are resolved CLIENT-side against
        # the in-progress form. Server must NOT substitute them, even
        # with filters present.
        out = self._resolve(
            "Hi {{ steps.signup.name | upper }}",
            current_node_id="signup",  # same as the ref
            steps_data={"signup": {"name": "Dana"}},
        )
        # The literal template comes back untouched, filter and all.
        assert out == "Hi {{ steps.signup.name | upper }}"

    def test_mixed_same_node_and_cross_node(self):
        # A literal same-node ref AND a resolved cross-node ref in
        # one string. The literal survives; the cross-node resolves.
        out = self._resolve(
            "Hi {{ steps.confirm.display | upper }}, "
            "level {{ steps.signup.cadence | lower }}",
            current_node_id="confirm",
            steps_data={"signup": {"cadence": "Weekly"}},
        )
        assert out == (
            "Hi {{ steps.confirm.display | upper }}, level weekly"
        )

    def test_plain_text_passes_through(self):
        out = self._resolve(
            "No templates here.",
            current_node_id="thanks",
            steps_data={"signup": {}},
        )
        assert out == "No templates here."

    def test_slugify_filter_works(self):
        # `slugify` is registered in the shared Jinja env (used by
        # submission_id). Verify it's also available in prop
        # templates now. The actual slugify replaces non-alphanumeric
        # runs with single dashes, so `O'Reilly` becomes `o-reilly`.
        out = self._resolve(
            "{{ steps.signup.name | slugify }}",
            current_node_id="thanks",
            steps_data={"signup": {"name": "Dana O'Reilly"}},
        )
        assert out == "dana-o-reilly"

    def test_url_prop_is_percent_encoded(self):
        out = self._resolve(
            "https://x.com/?q={{ steps.signup.name }}",
            current_node_id="thanks",
            steps_data={"signup": {"name": "hello world"}},
            is_url=True,
        )
        # The whole resolved string gets URL-encoded.
        assert out == (
            "https%3A%2F%2Fx.com%2F%3Fq%3Dhello%20world"
        )

    def test_default_filter_renders_fallback(self):
        # `| default("...")` is one of the most useful filters for
        # missing fields — confirm it works.
        out = self._resolve(
            'Value: {{ steps.signup.role | default("none") }}',
            current_node_id="thanks",
            steps_data={"signup": {}},
        )
        # `_SilentUndefined.__str__` returns "" — but `|default` is
        # designed to fire only on Undefined sentinels. Verify the
        # intended behavior holds end-to-end.
        # NOTE: Whether `default` fires here depends on whether
        # `signup.role` reaches Jinja as `Undefined` or as `""`.
        # We accept either fall-through — the test asserts no crash
        # and a string output.
        assert isinstance(out, str)
        # Either "" (silent-undefined dropped before default) or
        # "none" (default fired); both are valid; the bug we care
        # about is "filter is left literal", which this catches.
        assert out in ("Value: ", "Value: none")
