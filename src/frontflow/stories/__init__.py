"""Narrative data stories, rendered ahead of time.

A story is an `.xmd` document — Markdown whose code cells can be
executed — living in the workflow source tree beside the forms. It is
rendered to HTML by a deliberate act (`frontflow story render`), and the
server only ever serves the result.

**Why pre-rendered.** The queries behind a data-science report are often
long and expensive, and nothing about them wants to run when someone
opens a page. Rendering offline also keeps the runtime image free of
Node: xmd is a Node CLI, and it is needed in the authoring environment
and in CI, never in the server.

**Access control is folder placement.** A story under `finance/` is
visible to exactly the groups granted `finance`, using the same gate
that governs forms (`auth.accessible_form_folders` /
`auth.folder_is_accessible`). There is nothing new to administer.

**The rendered artifact is self-describing.** It carries a leading HTML
comment recording the hash of the source it came from, so the server can
say "this was rendered from an older version" rather than silently
serving something stale. One file, and the metadata cannot be separated
from the content it describes.

**A story is a page, not a fragment in the app.** It is served as its
own document and framed in a sandbox, so an author can write whatever
HTML, CSS and JavaScript they like — including loading their own
libraries — without any of it reaching frontflow. Isolation replaces
sanitising: the document gets an opaque origin, so it cannot read
frontflow's cookies, storage or DOM no matter what it contains. What it
costs is that the story cannot call back into frontflow either, which is
consistent with a page whose data was baked at render time.

**A failing cell does not fail the render.** xmd bakes the error into
the page as `<div class="xmd-output xmd-error">` and exits 0. Left
alone, that publishes a broken story quietly, so the count of error
cells is recorded in the header and reported by the CLI.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# The xmd executable. An env var rather than a hard-coded name because
# xmd is not on npm yet, so an authoring machine may well be pointing at
# a checkout: XMD_BIN="node /path/to/xmd/dist/cli.js".
XMD_BIN_ENV = "XMD_BIN"
DEFAULT_XMD_BIN = "xmd"

STORY_SUFFIX = ".xmd"
RENDERED_SUFFIX = ".html"

# The self-describing header. Kept to ONE line so reading it back is a
# line read rather than an HTML parse.
_HEADER_RE = re.compile(r"^<!--\s*frontflow-story\s+(\{.*?\})\s*-->")

# xmd marks a cell whose code raised with this class, and still exits 0.
_ERROR_MARKER = 'class="xmd-output xmd-error"'


class StoryError(RuntimeError):
    """Rendering failed. The message is meant for the author."""


@dataclass(frozen=True)
class RenderedStory:
    """The result of rendering one `.xmd` document."""

    html: str
    title: Optional[str]
    source_sha256: str
    rendered_at: str
    cell_errors: int
    # The document's whole frontmatter, not a chosen few keys. A
    # listing is only as expressive as the metadata it can sort and
    # group by, and which fields an author cares about is theirs to
    # decide — `date`, `categories`, `author`, or something this code
    # has never heard of.
    frontmatter: dict[str, Any] = field(default_factory=dict)

    @property
    def has_errors(self) -> bool:
        return self.cell_errors > 0


def xmd_command() -> list[str]:
    """The xmd invocation, split into argv.

    `XMD_BIN` may be a bare name or a command with arguments, so that
    pointing at a checkout ("node .../cli.js") needs no wrapper script.
    """
    raw = os.environ.get(XMD_BIN_ENV, DEFAULT_XMD_BIN).strip()
    return raw.split() if raw else [DEFAULT_XMD_BIN]


def xmd_available() -> bool:
    """Whether the xmd command can be found.

    Checked before rendering so the failure is 'xmd is not installed'
    rather than a FileNotFoundError from deep in subprocess.
    """
    argv = xmd_command()
    return shutil.which(argv[0]) is not None


def source_hash(text: str) -> str:
    """The identity of a story's source. Newlines normalised so a
    checkout with different line endings is not perpetually stale."""
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _run_xmd(path: Path, args: list[str]) -> str:
    argv = [*xmd_command(), "render", str(path), *args]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise StoryError(
            f"Could not run {argv[0]!r}. Install xmd, or point "
            f"{XMD_BIN_ENV} at a checkout — for example "
            f'{XMD_BIN_ENV}="node /path/to/xmd/dist/cli.js".'
        ) from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise StoryError(f"xmd failed on {path.name}: {detail}")
    return proc.stdout


def render(path: Path, *, run: bool = True) -> RenderedStory:
    """Render one `.xmd` file to an HTML fragment.

    `run=False` renders the prose and shows the code without executing
    it — useful for checking a document cheaply.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")

    html = _run_xmd(path, ["--run"] if run else [])

    title: Optional[str] = None
    frontmatter: dict[str, Any] = {}
    try:
        parsed = json.loads(_run_xmd(path, ["--frontmatter"]) or "{}")
        if isinstance(parsed, dict):
            frontmatter = parsed
            raw_title = parsed.get("title")
            if isinstance(raw_title, str) and raw_title.strip():
                title = raw_title.strip()
    except (StoryError, json.JSONDecodeError):
        # A document without frontmatter is perfectly valid; it just has
        # nothing to offer a listing. Not worth failing a good render.
        frontmatter = {}

    return RenderedStory(
        html=html,
        title=title,
        source_sha256=source_hash(text),
        rendered_at=datetime.now(timezone.utc).isoformat(),
        cell_errors=html.count(_ERROR_MARKER),
        frontmatter=frontmatter,
    )


def rendered_path_for(source: Path) -> Path:
    """Where `source`'s HTML lives — beside it, same stem."""
    return Path(source).with_suffix(RENDERED_SUFFIX)


def serialize(story: RenderedStory) -> str:
    """The artifact written to disk: header line, then the fragment."""
    header = {
        "source_sha256": story.source_sha256,
        "rendered_at": story.rendered_at,
        "title": story.title,
        "cell_errors": story.cell_errors,
        "frontmatter": story.frontmatter,
    }
    # separators= keeps it on one line, which _HEADER_RE depends on.
    return (
        "<!-- frontflow-story "
        + json.dumps(header, separators=(",", ":"))
        + " -->\n"
        + story.html
    )


def parse(rendered: str) -> tuple[dict[str, Any], str]:
    """Split a rendered artifact into its header and its HTML.

    A file with no header — hand-written, or produced by an older
    frontflow — yields an empty header rather than an error. It is
    servable; it just cannot be checked for staleness.
    """
    first, _, rest = rendered.partition("\n")
    match = _HEADER_RE.match(first.strip())
    if not match:
        return {}, rendered
    try:
        header = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}, rendered
    return (header if isinstance(header, dict) else {}), rest


def is_stale(rendered: str, source_text: str) -> Optional[bool]:
    """Whether `rendered` predates `source_text`.

    None means "cannot tell" — an artifact with no header. Distinct from
    False, because "no reason to think it is stale" and "checked, and it
    is current" are different claims to make to a reader.
    """
    header, _ = parse(rendered)
    recorded = header.get("source_sha256")
    if not isinstance(recorded, str) or not recorded:
        return None
    return recorded != source_hash(source_text)


def render_to_disk(source: Path, *, run: bool = True) -> tuple[Path, RenderedStory]:
    """Render `source` and write the artifact beside it."""
    story = render(source, run=run)
    target = rendered_path_for(source)
    target.write_text(serialize(story), encoding="utf-8")
    return target, story


def iter_story_sources(directory: Path) -> list[Path]:
    """Every `.xmd` under `directory`, using the source tree's own rules.

    `_`-prefixed files and directories are skipped, matching
    `sources.LocalDirSource` — the established way to park a file
    without deleting it.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return []
    found = []
    for path in sorted(directory.rglob(f"*{STORY_SUFFIX}")):
        rel = path.relative_to(directory)
        if any(part.startswith("_") for part in rel.parts):
            continue
        found.append(path)
    return found


# --- Serving a story as its own page --------------------------------------

# Enough styling that a plain Markdown story looks like a document
# rather than raw text. Deliberately modest, and deliberately FIRST in
# the shell: anything the author writes comes later and wins, so this is
# a starting point rather than a house style imposed on them.
_DEFAULT_STYLE = """
  :root { color-scheme: light dark; }
  body {
    margin: 0 auto; padding: 2rem 1.5rem; max-width: 46rem;
    font: 16px/1.6 ui-sans-serif, system-ui, -apple-system, sans-serif;
  }
  h1, h2, h3 { line-height: 1.25; margin: 1.5rem 0 .75rem; }
  h1 { font-size: 1.75rem; } h2 { font-size: 1.35rem; } h3 { font-size: 1.1rem; }
  body > *:first-child { margin-top: 0; }
  img { max-width: 100%; height: auto; }
  table { border-collapse: collapse; width: 100%; }
  th, td { border: 1px solid currentColor; padding: .375rem .625rem; text-align: left; }
  pre, code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .xmd-code, .xmd-output pre {
    font-size: .8125rem; overflow-x: auto; padding: .75rem .875rem; margin: 0;
  }
  .xmd-code { border: 1px solid currentColor; border-bottom: none; opacity: .9; }
  .xmd-output pre { border: 1px solid currentColor; margin-bottom: .75rem; }
  /* A cell that raised. xmd exits 0 either way, so this has to be loud. */
  .xmd-output.xmd-error pre { border-color: #b91c1c; color: #b91c1c; }
"""

# The story reports its own height so a `fit="content"` panel can size to
# it. A sandboxed document has an opaque origin and cannot be measured
# from outside, but it can still postMessage out — so the measurement
# has to come from within. Only added to documents frontflow wraps; an
# author supplying a whole document is left alone, and may post the same
# message themselves.
HEIGHT_MESSAGE = "frontflow:story-height"

_HEIGHT_REPORTER = """
  (function () {
    function report() {
      var h = Math.max(
        document.documentElement.scrollHeight, document.body.scrollHeight
      );
      parent.postMessage({ type: "%s", height: h }, "*");
    }
    addEventListener("load", report);
    if (window.ResizeObserver) new ResizeObserver(report).observe(document.body);
  })();
""" % HEIGHT_MESSAGE


def is_full_document(html: str) -> bool:
    """Whether the author already emitted a whole page.

    An author who wants complete control writes `<!doctype html>` in a
    raw `html` cell, and gets served exactly that — no shell, no
    injected styles, no height reporter.
    """
    head = html.lstrip()[:200].lower()
    return head.startswith("<!doctype") or head.startswith("<html")


def as_document(html: str, *, title: Optional[str] = None) -> str:
    """A story fragment as a standalone HTML document.

    Returned unchanged when the author already wrote a full page.
    """
    if is_full_document(html):
        return html

    safe_title = (title or "Story").replace("&", "&amp;").replace("<", "&lt;")
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{safe_title}</title>\n"
        f"<style>{_DEFAULT_STYLE}</style>\n"
        "</head>\n<body>\n"
        f"{html}\n"
        f"<script>{_HEIGHT_REPORTER}</script>\n"
        "</body>\n</html>\n"
    )
