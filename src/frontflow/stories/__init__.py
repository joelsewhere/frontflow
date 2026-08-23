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
from dataclasses import dataclass
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
    try:
        frontmatter = json.loads(_run_xmd(path, ["--frontmatter"]) or "{}")
        if isinstance(frontmatter, dict):
            raw_title = frontmatter.get("title")
            if isinstance(raw_title, str) and raw_title.strip():
                title = raw_title.strip()
    except (StoryError, json.JSONDecodeError):
        # A document without frontmatter is perfectly valid; it just has
        # no title to offer. Not worth failing a good render over.
        title = None

    return RenderedStory(
        html=html,
        title=title,
        source_sha256=source_hash(text),
        rendered_at=datetime.now(timezone.utc).isoformat(),
        cell_errors=html.count(_ERROR_MARKER),
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
