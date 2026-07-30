"""Tests for `S3Download.filename` — the kwarg that lets a form
author control the user-facing download filename.

End-to-end concern: the field is templated against the submission's
steps namespace (so a name like `{{ steps.property_name.name }}
report.csv` actually substitutes), and the download endpoint passes
a properly-built Content-Disposition through to the presigned URL.
"""
from __future__ import annotations

from frontflow.dsl.displays import S3Download
from frontflow.dsl.references import TEMPLATED_PROPS
from frontflow.main import _build_content_disposition


class TestS3DownloadFilenameKwarg:
    def test_filename_defaults_to_none(self):
        """Unset filename preserves today's behavior — endpoint won't
        attach a Content-Disposition, browser uses the URL basename."""
        d = S3Download(bucket="b", key="k.csv")
        assert d.filename is None

    def test_filename_accepted_and_stored(self):
        """Set value sticks around through compile/serialize."""
        d = S3Download(
            bucket="b", key="k.csv",
            filename="My Report.csv",
        )
        assert d.filename == "My Report.csv"

    def test_filename_is_in_templated_props(self):
        """If this assertion ever fails, `{{ steps.<x> }}` tokens in a
        filename will leak through to the wire instead of resolving
        against the submission. The endpoint would then pass the
        unresolved template into Content-Disposition and the browser
        would save the file as literally `My report {{ steps.x.y }}`.
        """
        assert "filename" in TEMPLATED_PROPS


class TestBuildContentDisposition:
    def test_ascii_filename_quoted_and_filename_star_set(self):
        """An ASCII filename produces both the legacy `filename="..."`
        parameter and the RFC 5987 `filename*=UTF-8''<encoded>` one.
        Both are emitted so legacy clients AND modern browsers
        cooperate; modern browsers prefer `filename*` when present."""
        cd = _build_content_disposition("report.csv")
        assert cd.startswith("attachment; ")
        assert 'filename="report.csv"' in cd
        assert "filename*=UTF-8''report.csv" in cd

    def test_filename_with_quote_is_escaped(self):
        """A `"` in the filename breaks the legacy quoted-string if
        unescaped — backslash-escape so the header stays valid."""
        cd = _build_content_disposition('hello "world".csv')
        assert 'filename="hello \\"world\\".csv"' in cd

    def test_filename_with_backslash_is_escaped(self):
        """A literal `\\` is the escape character in a quoted-string
        and has to be doubled."""
        cd = _build_content_disposition("path\\file.csv")
        assert 'filename="path\\\\file.csv"' in cd

    def test_non_ascii_in_filename_star_and_replaced_in_ascii(self):
        """Non-ASCII characters are percent-encoded in `filename*` and
        replaced (not omitted, so the legacy filename doesn't collapse
        to nothing if the name is all-Unicode) in the ASCII fallback.
        Modern browsers will pick `filename*` and render the original.
        """
        cd = _build_content_disposition("rapport-é.csv")
        # `é` -> %C3%A9 in UTF-8 percent-encoding.
        assert "filename*=UTF-8''rapport-%C3%A9.csv" in cd
        # ASCII fallback: `é` replaced with `_`.
        assert 'filename="rapport-_.csv"' in cd

    def test_filename_with_space_is_quoted_and_encoded(self):
        """A space is legal inside the quoted-string in the legacy
        parameter, but must be percent-encoded in `filename*` per RFC
        5987 — the encoded form uses %20."""
        cd = _build_content_disposition("Quarterly Report.csv")
        assert 'filename="Quarterly Report.csv"' in cd
        assert "filename*=UTF-8''Quarterly%20Report.csv" in cd

    def test_control_characters_stripped(self):
        """Newlines / NUL in a header value would let an attacker
        inject headers via a templated filename (e.g. one sourced
        from user-supplied data). Strip everything non-printable
        before serialization."""
        cd = _build_content_disposition("ok\r\nname.csv\x00")
        # Neither raw \r nor \n appears in the output.
        assert "\r" not in cd and "\n" not in cd and "\x00" not in cd
        # The visible part survived.
        assert "okname.csv" in cd
