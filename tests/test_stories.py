"""Narrative data stories — rendered ahead of time, served as HTML.

Two things are load-bearing here and get most of the attention:

* **The server never executes anything.** A story is rendered by a
  deliberate act; the server serves whatever was last written. If that
  ever changes, the runtime image needs Node and a sandboxing story.
* **Access control is folder placement.** A story under `finance/` is
  visible to exactly the groups granted `finance`, reusing the gate that
  governs forms. The failure direction that matters is a story leaking
  to someone outside its folder.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frontflow import main as main_mod
from frontflow import stories


# --- The artifact format -------------------------------------------------


class TestArtifactFormat:
    """The rendered file describes itself, so metadata cannot drift from
    the content it describes."""

    def _story(self, **kw):
        defaults = dict(
            html="<h1>Hi</h1>",
            title="Hi",
            source_sha256="abc123",
            rendered_at="2026-01-01T00:00:00+00:00",
            cell_errors=0,
        )
        defaults.update(kw)
        return stories.RenderedStory(**defaults)

    def test_serialize_then_parse_round_trips(self):
        art = stories.serialize(self._story())
        header, html = stories.parse(art)
        assert html == "<h1>Hi</h1>"
        assert header["source_sha256"] == "abc123"
        assert header["title"] == "Hi"

    def test_the_header_is_a_single_line(self):
        """`parse` reads one line rather than parsing HTML, so a header
        that wrapped would silently stop being found."""
        art = stories.serialize(self._story(html="<p>a</p>\n<p>b</p>"))
        assert len(art.split("\n")[0].splitlines()) == 1
        assert art.split("\n")[0].endswith("-->")

    def test_html_containing_comments_survives(self):
        art = stories.serialize(self._story(html="<!-- not the header -->\n<p>x</p>"))
        _, html = stories.parse(art)
        assert html == "<!-- not the header -->\n<p>x</p>"

    def test_a_file_with_no_header_is_still_servable(self):
        """Hand-written or from an older frontflow. Servable, just not
        checkable."""
        header, html = stories.parse("<p>plain</p>")
        assert header == {}
        assert html == "<p>plain</p>"

    def test_a_corrupt_header_degrades_rather_than_raising(self):
        header, html = stories.parse("<!-- frontflow-story {not json} -->\n<p>x</p>")
        assert header == {}
        assert "<p>x</p>" in html


class TestStaleness:
    def test_matching_source_is_not_stale(self):
        art = stories.serialize(
            stories.RenderedStory(
                html="<p>x</p>",
                title=None,
                source_sha256=stories.source_hash("# doc"),
                rendered_at="now",
                cell_errors=0,
            )
        )
        assert stories.is_stale(art, "# doc") is False

    def test_edited_source_is_stale(self):
        art = stories.serialize(
            stories.RenderedStory(
                html="<p>x</p>",
                title=None,
                source_sha256=stories.source_hash("# doc"),
                rendered_at="now",
                cell_errors=0,
            )
        )
        assert stories.is_stale(art, "# doc edited") is True

    def test_unknown_is_distinct_from_current(self):
        """None and False mean different things to a reader: 'cannot
        tell' is a weaker claim than 'checked, and current'."""
        assert stories.is_stale("<p>no header</p>", "# doc") is None

    def test_line_endings_do_not_cause_false_staleness(self):
        """A checkout with CRLF must not report every story as stale."""
        assert stories.source_hash("a\r\nb") == stories.source_hash("a\nb")


class TestSourceDiscovery:
    def test_underscore_files_are_skipped(self, tmp_path: Path):
        (tmp_path / "good.xmd").write_text("# ok")
        (tmp_path / "_wip.xmd").write_text("# draft")
        drafts = tmp_path / "_drafts"
        drafts.mkdir()
        (drafts / "nested.xmd").write_text("# draft")

        found = [p.name for p in stories.iter_story_sources(tmp_path)]
        assert found == ["good.xmd"]

    def test_a_missing_directory_yields_nothing(self, tmp_path: Path):
        assert stories.iter_story_sources(tmp_path / "nope") == []


# --- Serving and access control ------------------------------------------


def _register(name: str, folder: str, *, rendered=True, stale=False, html="<p>x</p>"):
    main_mod.STORIES[name] = {
        "name": name,
        "folder": folder,
        "rendered": rendered,
        "title": "A Story",
        "stale": stale,
        "cell_errors": 0,
        "rendered_at": "2026-01-01T00:00:00+00:00",
        "html": html,
    }


@pytest.fixture(autouse=True)
def clean_stories():
    saved = dict(main_mod.STORIES)
    main_mod.STORIES.clear()
    yield
    main_mod.STORIES.clear()
    main_mod.STORIES.update(saved)


class TestServing:
    def test_a_rendered_story_is_served(self, admin_client: TestClient):
        _register("finance/q.xmd", "finance", html="<h1>Q</h1>")
        r = admin_client.get("/api/stories/finance/q.xmd")
        assert r.status_code == 200, r.text
        assert r.json()["html"] == "<h1>Q</h1>"

    def test_an_unrendered_story_is_409_naming_the_fix(
        self, admin_client: TestClient
    ):
        """Not 404 — it exists and the caller may see it. It has simply
        never been built, and the message says how."""
        _register("finance/q.xmd", "finance", rendered=False, html=None)
        r = admin_client.get("/api/stories/finance/q.xmd")
        assert r.status_code == 409
        assert "story render" in r.json()["detail"]

    def test_a_missing_story_is_404(self, admin_client: TestClient):
        assert admin_client.get("/api/stories/nope.xmd").status_code == 404

    def test_staleness_is_reported_rather_than_hidden(
        self, admin_client: TestClient
    ):
        _register("q.xmd", "", stale=True)
        assert admin_client.get("/api/stories/q.xmd").json()["stale"] is True

    def test_the_index_lists_stories(self, admin_client: TestClient):
        _register("a.xmd", "")
        _register("finance/b.xmd", "finance")
        names = [s["name"] for s in admin_client.get("/api/stories").json()]
        assert names == ["a.xmd", "finance/b.xmd"]


class TestFolderAccessControl:
    """The direction that matters is a story reaching someone outside
    its folder."""

    def test_a_user_without_the_grant_cannot_read_it(
        self, user_client: TestClient
    ):
        _register("finance/secret.xmd", "finance", html="<p>numbers</p>")
        r = user_client.get("/api/stories/finance/secret.xmd")
        assert r.status_code == 404
        assert "numbers" not in r.text

    def test_absence_is_indistinguishable_from_no_access(
        self, user_client: TestClient
    ):
        """404 for both, so whether a document exists under `finance/`
        is not something an outsider can probe."""
        _register("finance/secret.xmd", "finance")
        denied = user_client.get("/api/stories/finance/secret.xmd")
        missing = user_client.get("/api/stories/finance/absent.xmd")
        assert denied.status_code == missing.status_code == 404
        assert denied.json() == missing.json()

    def test_a_user_without_the_grant_does_not_see_it_listed(
        self, user_client: TestClient
    ):
        _register("finance/secret.xmd", "finance")
        assert user_client.get("/api/stories").json() == []

    def test_an_admin_sees_everything(self, admin_client: TestClient):
        _register("finance/secret.xmd", "finance")
        assert len(admin_client.get("/api/stories").json()) == 1

    def test_a_granted_group_can_read_it(self, user_client: TestClient):
        from frontflow.dsl import auth, store

        _register("finance/secret.xmd", "finance", html="<p>numbers</p>")

        user = auth.get_user_by_username("user")
        group = auth.create_group("finance-readers")
        auth.add_member(group["id"], user.id)
        auth.add_grant(group["id"], "finance", "view")

        r = user_client.get("/api/stories/finance/secret.xmd")
        assert r.status_code == 200, r.text
        assert r.json()["html"] == "<p>numbers</p>"


class TestTheServerNeverExecutes:
    def test_serving_a_story_does_not_invoke_xmd(
        self, admin_client: TestClient, monkeypatch
    ):
        """The whole design rests on this. If serving ever shells out,
        the runtime image needs Node and a sandboxing answer."""
        called = []
        monkeypatch.setattr(
            stories.subprocess,
            "run",
            lambda *a, **k: called.append(a) or pytest.fail("xmd was invoked"),
        )
        _register("q.xmd", "")
        assert admin_client.get("/api/stories/q.xmd").status_code == 200
        assert admin_client.get("/api/stories").status_code == 200
        assert called == []


class TestStoryPanel:
    """`workspace.Story` — the panel declaration."""

    def _compile(self, panel):
        from frontflow.dsl.workspaces import _compile_panel

        return _compile_panel(panel)

    def test_a_story_panel_compiles(self):
        from frontflow import workspace

        blk = self._compile(workspace.Story("stories/q.xmd", min_height=600))
        assert blk["type"] == "story"
        assert blk["props"]["name"] == "stories/q.xmd"
        assert blk["props"]["min_height"] == 600

    def test_naming_the_rendered_html_is_refused(self):
        """One name for the thing across the DSL, the CLI and the index —
        and the .html is an artifact, not the document."""
        from frontflow import workspace

        with pytest.raises(ValueError, match="not the rendered HTML"):
            workspace.Story("stories/q.html")

    def test_an_empty_name_is_refused(self):
        from frontflow import workspace

        with pytest.raises(ValueError, match="needs a story path"):
            workspace.Story("   ")

    def test_the_panel_id_is_derived_from_the_name(self):
        from frontflow import workspace

        assert (
            self._compile(workspace.Story("a/b.xmd"))["id"] == "story-a/b.xmd"
        )


class TestStoryDocument:
    """A story is served as its own page, not spliced into the app."""

    def test_a_fragment_is_wrapped_in_a_document(self):
        doc = stories.as_document("<h1>Hi</h1>", title="Hi")
        assert doc.lstrip().startswith("<!doctype html>")
        assert "<h1>Hi</h1>" in doc

    def test_an_author_supplied_document_is_untouched(self):
        """Complete control means complete: no shell, no injected
        styles, no height reporter."""
        full = "<!doctype html><html><body>mine</body></html>"
        assert stories.as_document(full) == full
        assert stories.HEIGHT_MESSAGE not in stories.as_document(full)

    def test_a_leading_html_tag_also_counts_as_a_document(self):
        assert stories.is_full_document("  <html><body>x</body></html>")
        assert stories.is_full_document("<!DOCTYPE HTML>\n<html>")
        assert not stories.is_full_document("<h1>x</h1>")

    def test_the_title_is_escaped(self):
        doc = stories.as_document("<p>x</p>", title='</title><script>x</script>')
        assert "<script>" not in doc.split("</head>")[0]

    def test_author_markup_is_not_sanitised(self):
        """The whole point of the reversal. An author's script and style
        must survive — sanitising them away is what made a story
        useless as a page."""
        frag = "<style>.a{color:red}</style><script>window.x=1</script><p>hi</p>"
        doc = stories.as_document(frag)
        assert "<script>window.x=1</script>" in doc
        assert "<style>.a{color:red}</style>" in doc


class TestStoryPageIsolation:
    """Isolation is what replaced sanitising, so it is what needs
    guarding."""

    def test_the_page_is_served_as_html(self, admin_client: TestClient):
        _register("q.xmd", "", html="<h1>Q</h1>")
        r = admin_client.get("/api/story-page/q.xmd")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/html")
        assert "<h1>Q</h1>" in r.text

    def test_the_response_is_sandboxed(self, admin_client: TestClient):
        """As a HEADER, not only as an iframe attribute — otherwise
        opening the URL directly would run author HTML with full access
        to the signed-in session."""
        _register("q.xmd", "")
        csp = admin_client.get("/api/story-page/q.xmd").headers[
            "content-security-policy"
        ]
        assert "sandbox" in csp
        assert "allow-scripts" in csp

    def test_the_sandbox_never_grants_same_origin(
        self, admin_client: TestClient
    ):
        """`allow-scripts` plus `allow-same-origin` lets the document
        reach out and remove its own sandbox. Together they are worse
        than no sandbox at all, because they look like one."""
        _register("q.xmd", "")
        csp = admin_client.get("/api/story-page/q.xmd").headers[
            "content-security-policy"
        ]
        assert "allow-same-origin" not in csp

    def test_the_workspace_may_frame_it(self, admin_client: TestClient):
        """The app-wide default is frame-ancestors 'none', which would
        block the panel outright."""
        _register("q.xmd", "")
        r = admin_client.get("/api/story-page/q.xmd")
        assert "frame-ancestors 'self'" in r.headers["content-security-policy"]
        assert "x-frame-options" not in {k.lower() for k in r.headers}

    def test_other_routes_keep_the_restrictive_default(
        self, admin_client: TestClient
    ):
        """The exemption must not have widened anything else."""
        r = admin_client.get("/api/stories")
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers.get("X-Frame-Options") == "DENY"

    def test_the_page_honours_folder_access(self, user_client: TestClient):
        _register("finance/secret.xmd", "finance", html="<p>numbers</p>")
        r = user_client.get("/api/story-page/finance/secret.xmd")
        assert r.status_code == 404
        assert "numbers" not in r.text

    def test_an_unrendered_story_is_409_here_too(
        self, admin_client: TestClient
    ):
        _register("q.xmd", "", rendered=False, html=None)
        r = admin_client.get("/api/story-page/q.xmd")
        assert r.status_code == 409
