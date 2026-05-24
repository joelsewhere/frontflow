"""frontflow command-line interface.

    frontflow serve [SOURCE] [--host H] [--port P] [--reload]
                    [--env-file PATH]
    frontflow example list
    frontflow example install <name>... | --all  [--dest DIR] [--force]
    frontflow example seed   <form_id>... | --all
                             [--count N] [--days D] [--env-file PATH]
    frontflow version

`serve` points the app at a workflow source — a local directory or an
s3:// URI — and starts the server (API + bundled web UI on one port).
`--env-file` loads configuration from a .env file before startup.
`example` is the namespace for working with the bundled demo
workflows: list what's available, install (copy) source files into
your project, and seed the database with realistic submissions to
populate the analytics views.
"""

from __future__ import annotations

import os
import random
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.table import Table

_EXAMPLES = Path(__file__).parent / "examples"

# stdout for normal output; a separate stderr console for errors so
# they're styled distinctly and go to the right stream.
console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="frontflow",
    help="Author stateful multi-step web forms as Python files.",
    add_completion=False,
    no_args_is_help=True,
)

# Subcommand group for everything bundled-demo-related.
example_app = typer.Typer(
    name="example",
    help="Work with bundled example workflows (list / install / seed).",
    add_completion=False,
    no_args_is_help=True,
)
app.add_typer(example_app, name="example")


def _load_env_file(path_str: str) -> None:
    """Load environment variables from a .env file. Variables already
    set in the real environment win — the file fills in the rest. Must
    run before frontflow.main is imported, since config is read there
    at import time. Exits the process on a missing file."""
    env_path = Path(path_str).expanduser().resolve()
    if not env_path.is_file():
        err_console.print(
            f"[red]frontflow:[/red] env file not found: {env_path}"
        )
        raise typer.Exit(code=1)
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        err_console.print(
            "[red]frontflow:[/red] python-dotenv is required "
            "for --env-file"
        )
        raise typer.Exit(code=1)
    # override=False: an explicitly-exported variable beats the file.
    load_dotenv(dotenv_path=env_path, override=False)
    console.print(
        f"[dim]frontflow:[/dim] loaded environment from {env_path}"
    )


@app.command()
def serve(
    source: Optional[str] = typer.Argument(
        None,
        help=(
            "Where workflow .py files live — a directory path, or an "
            "s3://bucket/prefix URI. Defaults to WORKFLOW_SOURCE from "
            "the environment / --env-file, or the current directory. "
            "S3 needs the s3 extra: pip install 'frontflow[s3]'."
        ),
    ),
    host: str = typer.Option("127.0.0.1", help="Host to bind."),
    port: int = typer.Option(8000, help="Port to bind."),
    reload: bool = typer.Option(
        False, "--reload", help="Restart on code changes (development)."
    ),
    env_file: Optional[str] = typer.Option(
        None,
        "--env-file",
        help=(
            "Load configuration from a .env file before startup "
            "(AWS credentials, DATABASE_URL, FRONTFLOW_SECRET_KEY, …). "
            "Variables already set in the environment take precedence."
        ),
    ),
) -> None:
    """Run the frontflow server — the API and the bundled web UI."""
    # Load the env file first — before anything reads configuration.
    if env_file:
        _load_env_file(env_file)

    # Resolve the workflow source. Precedence:
    #   1. an explicit positional argument on the command line
    #   2. WORKFLOW_SOURCE in the environment (incl. the --env-file)
    #   3. the current directory
    resolved_source = source or os.environ.get("WORKFLOW_SOURCE") or "."

    # A local path must exist; an s3:// URI is validated lazily on scan.
    if not resolved_source.startswith("s3://"):
        local = Path(resolved_source).expanduser().resolve()
        if not local.is_dir():
            err_console.print(
                f"[red]frontflow:[/red] no such directory: {local}"
            )
            raise typer.Exit(code=1)
        resolved_source = str(local)

    # main.py reads WORKFLOW_SOURCE at import time — set it before import.
    os.environ["WORKFLOW_SOURCE"] = resolved_source

    import uvicorn

    console.print(
        f"[dim]frontflow:[/dim] serving workflows from "
        f"[bold]{resolved_source}[/bold]"
    )
    console.print(
        f"[dim]frontflow:[/dim] [link]http://{host}:{port}[/link]"
    )

    if reload:
        # --reload needs an import string so the reloader can re-import.
        uvicorn.run(
            "frontflow.main:app", host=host, port=port, reload=True
        )
    else:
        # Import the app object directly from the installed package, so
        # a stray ./src/frontflow/ in the working directory can't shadow
        # it — passing a string would have uvicorn re-resolve the import.
        from frontflow.main import app as fastapi_app

        uvicorn.run(fastapi_app, host=host, port=port)


def _discover_examples() -> dict[str, dict[str, Path]]:
    """Inventory the bundled example forms. Each entry: a `form`
    Path (the workflow .py) and an optional `seed` Path (a sibling
    `_seed.py` fixture). Files starting with `_` are internal and
    skipped; the `airflow_dags/` directory contains DAG files for
    Airflow, not workflows, and is handled separately on install."""
    found: dict[str, dict[str, Path]] = {}
    if not _EXAMPLES.is_dir():
        return found
    for src in sorted(_EXAMPLES.glob("*.py")):
        if src.name.startswith("_"):
            continue
        if src.name.endswith("_seed.py"):
            # Pair with its form. Hide standalone seed files from the
            # listing — they belong to a form, not on their own.
            continue
        name = src.stem  # "quickstart" from "quickstart.py"
        seed = _EXAMPLES / f"{name}_seed.py"
        found[name] = {
            "form": src,
            "seed": seed if seed.is_file() else None,
        }
    return found


@example_app.command(name="list")
def example_list() -> None:
    """List bundled example workflows."""
    examples = _discover_examples()
    if not examples:
        console.print("[dim]No bundled examples found.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("name", style="cyan")
    table.add_column("seed fixture")
    table.add_column("description")
    for name, paths in examples.items():
        # Pull the first non-empty docstring line as a description.
        # Falls back to the file name if the form is undocumented.
        desc = _first_doc_line(paths["form"]) or name
        seed = "yes" if paths["seed"] else "[dim]auto[/dim]"
        table.add_row(name, seed, desc)
    console.print(table)
    console.print(
        f"\n[dim]Install one with:[/dim] "
        f"[bold]frontflow example install <name> --dest <dir>[/bold]"
    )


def _first_doc_line(form_path: Path) -> Optional[str]:
    """Read the first non-empty docstring line of a workflow file.
    Returns None if the file has no docstring."""
    try:
        text = form_path.read_text(encoding="utf-8")
    except OSError:
        return None
    # crude but enough — find the first triple-quoted string, take its
    # first non-empty line. We don't need to parse Python.
    for quote in ('"""', "'''"):
        i = text.find(quote)
        if i == -1:
            continue
        j = text.find(quote, i + 3)
        if j == -1:
            continue
        block = text[i + 3:j]
        for line in block.splitlines():
            line = line.strip()
            if line:
                return line
        return None
    return None


@example_app.command(name="install")
def example_install(
    names: list[str] = typer.Argument(
        None,
        help="One or more example names. Run `example list` to see them.",
    ),
    dest: str = typer.Option(
        "./workflows",
        "--dest", "-d",
        help="Directory to copy the example files into.",
    ),
    all_: bool = typer.Option(
        False, "--all", help="Install every bundled example.",
    ),
    force: bool = typer.Option(
        False, "--force", "-f",
        help="Overwrite existing files instead of erroring.",
    ),
) -> None:
    """Copy bundled example workflow files into a directory.

    Forms land in `<dest>/examples/<name>.py` so they sit in their
    own subfolder, easy to keep separate from your own workflows and
    easy to remove in one shot. Seed fixtures land in
    `<dest>/examples/_seeds/<name>_seed.py` — out of the way for
    casual use, but available to edit when you customize a form's
    inputs and need the fixture to match.

    The Airflow DAG files used by Airflow-backed examples are copied
    to a sibling `frontflow-example-dags/` dir so they don't get
    scanned as workflows.
    """
    examples = _discover_examples()
    if all_:
        if names:
            err_console.print(
                "[red]frontflow:[/red] pass names OR --all, not both."
            )
            raise typer.Exit(code=1)
        names = list(examples.keys())
    if not names:
        err_console.print(
            "[red]frontflow:[/red] no examples named — pass names or "
            "--all. Run `frontflow example list` to see options."
        )
        raise typer.Exit(code=1)

    # Validate every requested name exists before copying anything,
    # so a typo in one name doesn't leave a half-populated dir.
    unknown = [n for n in names if n not in examples]
    if unknown:
        err_console.print(
            f"[red]frontflow:[/red] unknown example(s): "
            f"{', '.join(unknown)}"
        )
        raise typer.Exit(code=1)

    dest_dir = Path(dest).expanduser().resolve()
    examples_dir = dest_dir / "examples"
    seeds_dir = examples_dir / "_seeds"
    examples_dir.mkdir(parents=True, exist_ok=True)
    seeds_dir.mkdir(parents=True, exist_ok=True)

    # Check conflicts before copying so we fail atomically.
    conflicts: list[Path] = []
    for name in names:
        targets = [examples_dir / f"{name}.py"]
        if examples[name]["seed"]:
            targets.append(seeds_dir / f"{name}_seed.py")
        for t in targets:
            if t.exists() and not force:
                conflicts.append(t)
    if conflicts:
        err_console.print(
            f"[red]frontflow:[/red] {len(conflicts)} file(s) already "
            f"exist (use --force to overwrite):"
        )
        for c in conflicts:
            err_console.print(f"  {c}")
        raise typer.Exit(code=1)

    copied_forms: list[Path] = []
    copied_seeds: list[Path] = []
    for name in names:
        paths = examples[name]
        form_target = examples_dir / paths["form"].name
        shutil.copy2(paths["form"], form_target)
        copied_forms.append(form_target)
        if paths["seed"]:
            seed_target = seeds_dir / paths["seed"].name
            shutil.copy2(paths["seed"], seed_target)
            copied_seeds.append(seed_target)

    total = len(copied_forms) + len(copied_seeds)
    console.print(
        f"[dim]frontflow:[/dim] copied [bold]{total}[/bold] file(s) "
        f"to {examples_dir}"
    )
    for f in copied_forms:
        console.print(f"  [cyan]{f.relative_to(dest_dir)}[/cyan]")
    for f in copied_seeds:
        console.print(f"  [dim]{f.relative_to(dest_dir)}[/dim]")

    # If any installed example used Airflow operators, copy the
    # bundled DAGs alongside. Goes in `examples/_airflow_dags/` so:
    #   (a) it's cleaned up when you `rm -r examples/`
    #   (b) the `_`-prefixed dir is skipped by `serve`'s workflow
    #       scanner — these files would crash the import otherwise
    #       (they're Airflow DAGs, not frontflow workflows).
    dag_src = _EXAMPLES / "airflow_dags"
    needs_dags = any(
        "TriggerDag" in (examples[n]["form"].read_text(encoding="utf-8"))
        or "TaskSensor" in examples[n]["form"].read_text(encoding="utf-8")
        for n in names
    )
    if dag_src.is_dir() and needs_dags:
        dag_dest = examples_dir / "_airflow_dags"
        shutil.copytree(dag_src, dag_dest, dirs_exist_ok=True)
        console.print(
            f"\nBundled Airflow DAGs copied to {dag_dest}\n"
            f"  [dim](The bundled examples use connection=\"mock\" so they"
            f" run without real Airflow. Deploy these DAGs to your"
            f" Airflow dags/ folder only if you switch the examples to"
            f" a real connection.)[/dim]"
        )

    console.print(
        f"\nServe the workflows with:\n  "
        f"[bold]frontflow serve {dest_dir}[/bold]"
    )


def _load_seed_scenarios(form_name: str) -> Optional[list[dict]]:
    """Read SCENARIOS from a bundled or user-installed `_seed.py`.

    Lookup order:
      1. The bundled fixture inside the package
         (`frontflow/examples/<form>_seed.py`).
      2. The user's installed `_seeds/` folder relative to the
         workflow source — matches `example install`'s layout
         (`<dest>/examples/_seeds/<form>_seed.py`). Works whether
         WORKFLOW_SOURCE points at `<dest>/examples/` (so seeds
         live one level up via the `_seeds` sibling) or at the
         parent `<dest>/` (seeds live at `examples/_seeds/`).
      3. Sibling fixture next to the form file — back-compat with
         the pre-subfolder install layout.

    First hit wins. Returns None if nothing found — the seeder
    falls back to auto-fill.
    """
    import importlib.util
    candidates: list[Path] = [_EXAMPLES / f"{form_name}_seed.py"]
    src = os.environ.get("WORKFLOW_SOURCE")
    if src and not src.startswith("s3://"):
        src_path = Path(src)
        candidates.extend([
            src_path / "_seeds" / f"{form_name}_seed.py",
            src_path / "examples" / "_seeds" / f"{form_name}_seed.py",
            src_path / f"{form_name}_seed.py",  # back-compat sibling
        ])
    for cand in candidates:
        if not cand.is_file():
            continue
        spec = importlib.util.spec_from_file_location(
            f"_seed_{form_name}", cand
        )
        if spec is None or spec.loader is None:
            continue
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        scenarios = getattr(mod, "SCENARIOS", None)
        if isinstance(scenarios, list) and scenarios:
            return scenarios
    return None


def _auto_fill_node(node: Any) -> dict[str, Any]:
    """Best-effort default values for a node's fields when no seed
    fixture is available. Each input type gets a type-appropriate
    placeholder so the submission succeeds validation. Skips
    unnamed fields (the runtime accepts them but we can't address
    them by id)."""
    today = datetime.now(timezone.utc).date().isoformat()
    out: dict[str, Any] = {}
    for fld in node.fields:
        if not fld.name:
            continue
        t = fld.type
        if t == "checkbox":
            out[fld.name] = bool(fld.required)
        elif t in ("text", "tel", "url", "email"):
            out[fld.name] = "sample"
        elif t == "textarea":
            out[fld.name] = "Lorem ipsum."
        elif t in ("number", "integer"):
            out[fld.name] = 42
        elif t == "slider":
            out[fld.name] = 50
        elif t == "rating":
            out[fld.name] = 4
        elif t == "date":
            out[fld.name] = today
        elif t == "time":
            out[fld.name] = "12:00"
        elif t in ("radio", "select"):
            opts = fld.options or []
            out[fld.name] = opts[0] if opts else "sample"
        elif t == "multi_select":
            opts = fld.options or []
            out[fld.name] = opts[:1]
        elif t == "checkbox_list":
            out[fld.name] = []
        elif t == "date_range":
            out[fld.name] = {"start": today, "end": today}
        elif t == "number_range":
            out[fld.name] = {"min": 0, "max": 100}
        elif t == "checkbox_grid":
            # Without knowing rows/columns we can't fill it; leave
            # empty and let the runtime reject if required.
            out[fld.name] = {}
        # file / s3file / sankey types are skipped — they need a
        # blob to upload which the seeder can't fabricate.
    return out


def _pick_scenario(scenarios: list[dict]) -> dict:
    """Weighted random pick. Weights need not sum to 1."""
    total = sum(s.get("weight", 1.0) for s in scenarios)
    r = random.random() * total
    acc = 0.0
    for s in scenarios:
        acc += s.get("weight", 1.0)
        if r <= acc:
            return s
    return scenarios[-1]


def _interpolate(value: Any, i: int, form_id: str = "") -> Any:
    """Substitute the per-submission counter into scenario values.
    Strings containing `{i}` get `i` interpolated (prefixed with a
    short form-id discriminator so two forms' fixtures with the
    same names don't collide on globally-unique submission ids
    minted from those names). Other values pass through.
    """
    if isinstance(value, str):
        if "{i}" in value:
            # Discriminator: short prefix derived from the form id.
            # Combined with the iteration counter, this makes any
            # `{i}`-bearing string unique across the seed run, so
            # workflows that mint submission_ids from name-like
            # fields don't collide across forms.
            disc = f"{form_id[:3]}{i}" if form_id else str(i)
            return value.replace("{i}", disc)
        return value
    if isinstance(value, dict):
        return {k: _interpolate(v, i, form_id) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v, i, form_id) for v in value]
    return value


def _seed_one_submission(
    client: Any,
    form_id: str,
    form: Any,
    scenario: dict,
    iteration: int = 0,
) -> tuple[Optional[str], list[str]]:
    """Create a single submission per the scenario and walk it
    through the requested nodes. Returns (submission_id, submitted
    node ids). The submission_id may be None for scenarios that
    never reach the landing step (advance_to=None with no values).
    """
    advance_to = scenario.get("advance_to")
    values = _interpolate(scenario.get("values", {}), iteration, form_id)
    node_ids = list(form.all_nodes_by_id.keys())
    landing_id = node_ids[0] if node_ids else None
    if advance_to is None and not values:
        # Scenario explicitly opted out of creating a submission.
        return None, []
    landing_vals = values.get(landing_id, {}) if landing_id else {}
    if landing_id and not landing_vals:
        landing_vals = _auto_fill_node(form.all_nodes_by_id[landing_id])
    r = client.post(
        f"/api/forms/{form_id}/submissions", json={"values": landing_vals}
    )
    if r.status_code >= 400:
        return None, []
    body = r.json()
    # Use the handle (always present) — not the minted submission_id,
    # which can be a slugified name. Downstream `_backdate_submission`
    # queries by handle. Subsequent step POSTs accept either handle
    # or submission_id, so we pass minted-id-or-handle for those (the
    # minted id is more user-recognizable in logs).
    handle = body.get("handle")
    sid_for_steps = body.get("submission_id") or handle
    if not handle:
        return None, []
    submitted = [landing_id] if landing_id else []
    if advance_to is None or advance_to == landing_id:
        return handle, submitted
    try:
        stop_at = node_ids.index(advance_to)
    except ValueError:
        return handle, submitted  # named node not in this form
    for nid in node_ids[1:stop_at + 1]:
        node = form.all_nodes_by_id[nid]
        vals = values.get(nid, {})
        if not vals and node.fields:
            vals = _auto_fill_node(node)
        r = client.post(
            f"/api/forms/{form_id}/submissions/{sid_for_steps}/steps/{nid}",
            json={"values": vals},
        )
        if r.status_code >= 400:
            break
        submitted.append(nid)
        # Stop walking if the submission already terminated. A form
        # with a `@backend.branch` to END (or to a node that
        # auto-completes) finishes inside one step submission — the
        # next iterations would 400 against nodes that aren't
        # awaiting. Honoring the terminal state also lets the sankey
        # see real branch divergence: low-amount and high-amount
        # paths actually end at different points.
        try:
            body = r.json()
            if body.get("state") in ("success", "failed"):
                break
        except Exception:
            pass
    return handle, submitted


def _finalize_mock_airflow_in_db(handle: str) -> None:
    """Mark every mock-Airflow operator on this submission's steps
    as `success` in the persisted external_state, and roll the
    submission state forward to `success` if every step is complete.

    Why: mock Airflow operators (`connection="mock"`) progress on a
    time schedule that the runtime checks at `advance()` time. The
    seeder doesn't wait for the schedule; it submits and returns.
    The submissions end up persisted with their mock operators
    stuck in `queued`/`running`. Time charts default-filter to
    success+failed, so seeded Airflow forms appear empty in the
    analytics views unless their state is moved to terminal.

    This is a DIRECT DB WRITE — bypasses the runtime entirely.
    Acknowledged hack contained to the seeder. After this runs the
    in-memory runtime object (if loaded) is stale; the seeder
    runs inside its own TestClient lifetime, so this is fine.
    """
    from sqlalchemy import select, update
    from frontflow.dsl import store as _store

    with _store._engine.begin() as conn:
        steps = conn.execute(
            select(
                _store.Step.id,
                _store.Step.seq,
                _store.Step.node_id,
                _store.Step.external_state,
                _store.Step.state,
                _store.Step.submitted_at,
            )
            .where(_store.Step.submission_handle == handle)
            .order_by(_store.Step.seq)
        ).fetchall()

        for sid_, seq_, node_id, ext_state, st_state, submitted_ in steps:
            ext = dict(ext_state or {})
            mutated = False
            for task_id, task_info in list(ext.items()):
                if not isinstance(task_info, dict):
                    continue
                if task_info.get("state") in ("success", "failed"):
                    continue
                # Set to success. `return` empty since the mock
                # operator typically has no real return value to
                # surface to chain consumers; this is enough for
                # state-based analytics filters.
                ext[task_id] = {
                    **task_info,
                    "state": "success",
                    "detail": "(mock — finalized by seeder)",
                }
                mutated = True
            if mutated:
                conn.execute(
                    update(_store.Step)
                    .where(_store.Step.id == sid_)
                    .values(external_state=ext)
                )
            if st_state != "submitted" and submitted_ is not None:
                # If the step had submitted_at set, the only reason
                # it'd be non-"submitted" is failure; leave failed
                # ones alone. Otherwise mark it submitted now that
                # the mock operators are done.
                if st_state == "awaiting":
                    conn.execute(
                        update(_store.Step)
                        .where(_store.Step.id == sid_)
                        .values(state="submitted")
                    )

        # Roll the submission state forward if every step is done.
        all_done = all(
            row.state in ("submitted", "failed")
            for row in conn.execute(
                select(_store.Step.state)
                .where(_store.Step.submission_handle == handle)
            ).fetchall()
        )
        if all_done:
            sub_row = conn.execute(
                select(_store.Submission.terminated_at)
                .where(_store.Submission.handle == handle)
            ).first()
            terminated_at = sub_row[0] if sub_row else None
            conn.execute(
                update(_store.Submission)
                .where(_store.Submission.handle == handle)
                .values(
                    state="success",
                    terminated_at=terminated_at
                    or datetime.now(timezone.utc),
                )
            )


def _backdate_submission(
    handle: str, created_at: datetime,
    jitter_step_durations: bool = True,
) -> None:
    """Shift a submission's + its steps' + events' timestamps by a
    delta so the analytics charts span the seed window. Direct DB
    write because the runtime has no "this submission actually
    started 12 days ago" hook — an acknowledged seeder hack
    contained to this function.

    If `jitter_step_durations` is true, ALSO inject random per-step
    durations (10s–5min each) so the "time per step" chart shows
    realistic non-zero bars. Without this, all step timestamps
    cluster at the submission's wall-clock arrival time (because
    the seeder posts each step's values in immediate succession)
    and the chart shows 0 seconds across the board. Step `started_at`
    chains forward from the previous step's `submitted_at` so the
    overall submission duration is the sum of step durations + the
    inter-step gaps (typically a few seconds each, from real submit
    latency).
    """
    from sqlalchemy import select, update
    from frontflow.dsl import store as _store

    with _store._engine.begin() as conn:
        sub_row = conn.execute(
            select(
                _store.Submission.created_at,
                _store.Submission.terminated_at,
                _store.Submission.updated_at,
            ).where(_store.Submission.handle == handle)
        ).first()
        if sub_row is None:
            return
        current_created = sub_row[0]
        if current_created.tzinfo is None:
            current_created = current_created.replace(tzinfo=timezone.utc)
        delta = created_at - current_created
        # No-op only if both the global shift AND the per-step jitter
        # are skipped. With jitter on, we still want to fan out step
        # timestamps even if the submission was created at the
        # target instant.
        if delta == timedelta(0) and not jitter_step_durations:
            return

        def shift(dt: Optional[datetime]) -> Optional[datetime]:
            if dt is None:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt + delta

        # Steps in chronological order — we walk them to inject
        # per-step durations on top of the global shift.
        step_rows = conn.execute(
            select(
                _store.Step.id,
                _store.Step.seq,
                _store.Step.started_at,
                _store.Step.submitted_at,
            )
            .where(_store.Step.submission_handle == handle)
            .order_by(_store.Step.seq)
        ).fetchall()

        # Compute new step timestamps. Start the first step at the
        # shifted started_at; each subsequent step's started_at
        # chains from the previous step's new submitted_at (plus a
        # tiny gap to mimic real submit latency).
        sub_started = shift(sub_row[0])
        new_step_times: list[tuple[int, datetime, Optional[datetime]]] = []
        cursor = sub_started
        for sid_, seq_, started_, submitted_ in step_rows:
            new_started = cursor
            if jitter_step_durations:
                # 10s–5min per step. Heavy tail so a few steps look
                # long ("waited on a human") and most are quick.
                # Lognormal with mu/sigma tuned to keep the median
                # near a minute and the long tail under 10min.
                dur_s = int(min(600, max(10, random.lognormvariate(4.0, 0.9))))
            else:
                dur_s = 0
            new_submitted = (
                new_started + timedelta(seconds=dur_s)
                if submitted_ is not None
                else None
            )
            new_step_times.append((sid_, new_started, new_submitted))
            # Inter-step gap: 1–5s of real submit latency.
            if new_submitted is not None:
                cursor = new_submitted + timedelta(
                    seconds=1 + random.random() * 4
                )

        # The submission's terminated_at, if any, becomes the LAST
        # step's submitted_at (the moment the run actually ended,
        # accounting for the new step durations).
        new_terminated = None
        if sub_row[1] is not None and new_step_times:
            last_submitted = new_step_times[-1][2]
            if last_submitted is not None:
                new_terminated = last_submitted

        conn.execute(
            update(_store.Submission)
            .where(_store.Submission.handle == handle)
            .values(
                created_at=sub_started,
                terminated_at=new_terminated or shift(sub_row[1]),
                updated_at=shift(sub_row[2]),
            )
        )
        for sid_, started_, submitted_ in new_step_times:
            conn.execute(
                update(_store.Step)
                .where(_store.Step.id == sid_)
                .values(started_at=started_, submitted_at=submitted_)
            )
        # Events get the simple global shift — fine-grained event
        # times are less interesting for the analytics views and
        # synthesizing realistic event timing is more work than it's
        # worth here. The relative ordering is preserved.
        for ev_row in conn.execute(
            select(_store.Event.id, _store.Event.occurred_at)
            .where(_store.Event.submission_handle == handle)
        ).fetchall():
            conn.execute(
                update(_store.Event)
                .where(_store.Event.id == ev_row[0])
                .values(occurred_at=shift(ev_row[1]))
            )


@example_app.command(name="seed")
def example_seed(
    form_ids: list[str] = typer.Argument(
        None, help="One or more form ids to seed.",
    ),
    all_: bool = typer.Option(
        False, "--all", help="Seed every form currently being served.",
    ),
    count: int = typer.Option(
        100, "--count", "-n",
        help="Number of submissions per form.",
    ),
    days: int = typer.Option(
        30, "--days", "-d",
        help="Spread submissions across this many days (backdated).",
    ),
    env_file: Optional[str] = typer.Option(
        None,
        "--env-file",
        help=(
            "Load configuration before connecting (DATABASE_URL, "
            "DB_PATH, FRONTFLOW_HOME, …). Use the same env file you "
            "pass to `frontflow serve` — otherwise the seed lands in "
            "a different database than the server reads."
        ),
    ),
    workflow_source: Optional[str] = typer.Option(
        None,
        "--source",
        help=(
            "Workflow source directory (overrides WORKFLOW_SOURCE). "
            "Defaults to the bundled examples so `seed --all` works "
            "out of the box."
        ),
    ),
    seed_random: Optional[int] = typer.Option(
        None,
        "--seed-random",
        help="Seed Python's random module for reproducible seeding.",
    ),
    reset: bool = typer.Option(
        False,
        "--reset",
        help=(
            "Before seeding each form, delete every existing "
            "submission for it. Makes re-seeding idempotent — useful "
            "for demo / dev scripts that need to start fresh on each "
            "run. Production data is destroyed; don't use on a live DB."
        ),
    ),
) -> None:
    """Populate the database with realistic submissions for analytics.

    Each form's `<form>_seed.py` fixture (if present) supplies named
    scenarios with weights; the seeder samples them per submission.
    Forms without a fixture get a generic type-based auto-fill (every
    submission identical — analytics will look uniform). Created-at
    timestamps are spread across the last --days days via direct DB
    writes so the time-series charts show a curve.

    Database resolution: same as `serve`. Pass --env-file to make sure
    you're writing to the database your server reads from — without
    it, you'll get the default SQLite file at ~/.frontflow/forms.db,
    which is probably not where production data lives.
    """
    if env_file:
        _load_env_file(env_file)
    if seed_random is not None:
        random.seed(seed_random)

    # Default the workflow source to the bundled examples — the
    # common case of seeding the bundled forms doesn't need the
    # caller to remember to point at them.
    resolved_source = (
        workflow_source
        or os.environ.get("WORKFLOW_SOURCE")
        or str(_EXAMPLES)
    )
    os.environ["WORKFLOW_SOURCE"] = resolved_source

    # Import the app AFTER setting WORKFLOW_SOURCE — main.py reads
    # it at import time.
    from fastapi.testclient import TestClient
    from frontflow.dsl import auth, store
    import frontflow.main as ff_main  # FORMS is rebound on startup,
    # so reference via the module — a `from … import FORMS` would
    # bind the original (empty) dict and never see the new one.

    store.init_db()
    # Print where we resolved the DB to, so a mismatch with `serve`'s
    # view is immediately obvious. Without this, a forgotten
    # `--env-file` silently writes to the wrong database.
    console.print(
        f"[dim]frontflow:[/dim] using database "
        f"[bold]{store.DATABASE_URL}[/bold]"
    )
    console.print(
        f"[dim]frontflow:[/dim] using workflow source "
        f"[bold]{resolved_source}[/bold]"
    )
    # Seeder needs an authenticated session; create or reuse a
    # dedicated seeder admin. Idempotent on re-run.
    seeder_username = "seeder"
    seeder_password = "seeder-only-used-locally"
    try:
        auth.create_user(seeder_username, seeder_password, is_admin=True)
    except ValueError:
        pass  # exists already

    if all_ and form_ids:
        err_console.print(
            "[red]frontflow:[/red] pass form ids OR --all, not both."
        )
        raise typer.Exit(code=1)

    with TestClient(ff_main.app) as client:
        r = client.post(
            "/api/auth/login",
            json={"username": seeder_username, "password": seeder_password},
        )
        if r.status_code >= 400:
            err_console.print(
                f"[red]frontflow:[/red] seeder login failed: "
                f"{r.status_code}"
            )
            raise typer.Exit(code=1)

        served = list(ff_main.FORMS.keys())
        if all_:
            form_ids = served
        if not form_ids:
            err_console.print(
                "[red]frontflow:[/red] no form ids — pass ids or --all. "
                f"Currently served: {', '.join(served) or '(none)'}"
            )
            raise typer.Exit(code=1)
        unknown = [fid for fid in form_ids if fid not in ff_main.FORMS]
        if unknown:
            err_console.print(
                f"[red]frontflow:[/red] unknown form id(s): "
                f"{', '.join(unknown)}. Served: {', '.join(served)}"
            )
            raise typer.Exit(code=1)

        for fid in form_ids:
            form = ff_main.FORMS[fid]
            if reset:
                wiped = store.delete_submissions_for_form(fid)
                if wiped:
                    # Evict the wiped submissions from the runtime's
                    # in-memory caches too — `hydrate_state` populated
                    # them at startup, so without this the seeder's
                    # fresh mints would collide on stale entries.
                    from frontflow.dsl import runtime as _runtime
                    with _runtime._submissions_lock:
                        for handle, sid in wiped:
                            _runtime._submissions.pop(handle, None)
                            if sid:
                                _runtime._id_index.pop(sid, None)
                    console.print(
                        f"[dim]frontflow:[/dim] {fid}: "
                        f"[bold]{len(wiped)}[/bold] prior "
                        "submission(s) wiped before reseed"
                    )
            scenarios = _load_seed_scenarios(fid)
            if scenarios is None:
                node_ids = list(form.all_nodes_by_id.keys())
                last = node_ids[-1] if node_ids else None
                scenarios = [{
                    "weight": 1.0,
                    "advance_to": last,
                    "values": {},
                }]
                console.print(
                    f"[dim]frontflow:[/dim] {fid}: no _seed.py — "
                    f"using auto-fill"
                )

            now = datetime.now(timezone.utc)
            window = timedelta(days=days)
            success = 0
            for i in range(count):
                scenario = _pick_scenario(scenarios)
                sid, submitted = _seed_one_submission(
                    client, fid, form, scenario, iteration=i,
                )
                if sid is None:
                    continue
                offset = timedelta(
                    seconds=random.random() * window.total_seconds()
                )
                created_at = now - window + offset
                _backdate_submission(sid, created_at)
                # Roll mock Airflow operators to success in the DB so
                # the analytics views (which default-filter to
                # success+failed) include seeded Airflow-form data.
                # No-op for forms without mock operators.
                _finalize_mock_airflow_in_db(sid)
                success += 1
            console.print(
                f"[dim]frontflow:[/dim] {fid}: seeded "
                f"[bold]{success}[/bold] / {count} submission(s) "
                f"across the last {days} day(s)"
            )


@app.command()
def version() -> None:
    """Print the frontflow version."""
    from frontflow import __version__

    console.print(f"frontflow [bold]{__version__}[/bold]")


@app.command(name="create-admin")
def create_admin(
    username: Optional[str] = typer.Option(
        None, help="Admin username (prompted if omitted)."
    ),
    password: Optional[str] = typer.Option(
        None, help="Admin password (prompted, hidden, if omitted)."
    ),
    env_file: Optional[str] = typer.Option(
        None,
        "--env-file",
        help="Load configuration (e.g. DATABASE_URL) before connecting.",
    ),
    reset_password: bool = typer.Option(
        False,
        "--reset-password",
        help=(
            "If the user already exists, set its password to the given "
            "value (and promote to admin if needed). Without this flag, "
            "an existing username is an error."
        ),
    ),
) -> None:
    """Create an admin account for the console.

    Run this once to bootstrap — the admin console refuses to serve
    until an account exists. Creating additional admins is allowed; an
    existing username is an error unless --reset-password is passed,
    in which case the user's password is overwritten and the account
    is promoted to admin if it wasn't already.
    """
    # Honor the same DB configuration as `serve`.
    if env_file:
        _load_env_file(env_file)

    if not username:
        username = typer.prompt("Admin username")
    if not password:
        password = typer.prompt(
            "Admin password", hide_input=True, confirmation_prompt=True
        )

    from frontflow.dsl import store
    from frontflow.dsl import auth

    store.init_db()
    existing = auth.get_user_by_username(username)
    if existing is not None and not reset_password:
        err_console.print(
            f"[red]frontflow:[/red] user {username!r} already exists. "
            f"Pass --reset-password to overwrite its password."
        )
        raise typer.Exit(code=1)
    if existing is not None:
        # Idempotent path — reset the password and promote to admin.
        auth.set_user_password(existing.id, password)
        if not existing.is_admin:
            auth.set_user_admin(existing.id, True)
            console.print(
                f"[dim]frontflow:[/dim] promoted "
                f"[bold]{existing.username}[/bold] to admin and "
                f"reset its password"
            )
        else:
            console.print(
                f"[dim]frontflow:[/dim] reset password for admin "
                f"[bold]{existing.username}[/bold]"
            )
        return
    try:
        user = auth.create_user(username, password, is_admin=True)
    except ValueError as e:
        err_console.print(f"[red]frontflow:[/red] {e}")
        raise typer.Exit(code=1)
    console.print(
        f"[dim]frontflow:[/dim] created admin "
        f"[bold]{user.username}[/bold]"
    )


@app.command(name="create-user")
def create_user(
    username: Optional[str] = typer.Option(
        None, help="Username (prompted if omitted)."
    ),
    password: Optional[str] = typer.Option(
        None, help="Password (prompted, hidden, if omitted)."
    ),
    env_file: Optional[str] = typer.Option(
        None,
        "--env-file",
        help="Load configuration (e.g. DATABASE_URL) before connecting.",
    ),
    reset_password: bool = typer.Option(
        False,
        "--reset-password",
        help=(
            "If the user already exists, set its password to the given "
            "value. Without this flag, an existing username is an error. "
            "Admin status is left unchanged either way."
        ),
    ),
) -> None:
    """Create a non-admin user account.

    A non-admin sees only the forms in folders a group grant covers.
    Grant access by adding the user to a group in the console's
    Access page. An existing username is an error unless
    --reset-password is passed, in which case the password is
    overwritten (admin status is preserved — this command will
    not demote an admin).
    """
    if env_file:
        _load_env_file(env_file)

    if not username:
        username = typer.prompt("Username")
    if not password:
        password = typer.prompt(
            "Password", hide_input=True, confirmation_prompt=True
        )

    from frontflow.dsl import store
    from frontflow.dsl import auth

    store.init_db()
    existing = auth.get_user_by_username(username)
    if existing is not None and not reset_password:
        err_console.print(
            f"[red]frontflow:[/red] user {username!r} already exists. "
            f"Pass --reset-password to overwrite its password."
        )
        raise typer.Exit(code=1)
    if existing is not None:
        auth.set_user_password(existing.id, password)
        console.print(
            f"[dim]frontflow:[/dim] reset password for "
            f"[bold]{existing.username}[/bold]"
            + (" (admin)" if existing.is_admin else "")
        )
        return
    try:
        user = auth.create_user(username, password, is_admin=False)
    except ValueError as e:
        err_console.print(f"[red]frontflow:[/red] {e}")
        raise typer.Exit(code=1)
    console.print(
        f"[dim]frontflow:[/dim] created user "
        f"[bold]{user.username}[/bold]"
    )


def main() -> None:
    """Console-script entry point — invokes the Typer app."""
    app()


if __name__ == "__main__":
    main()
