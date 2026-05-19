"""frontflow command-line interface.

    frontflow serve [SOURCE] [--host H] [--port P] [--reload]
                    [--env-file PATH]
    frontflow examples [DEST]
    frontflow version

`serve` points the app at a workflow source — a local directory or an
s3:// URI — and starts the server (API + bundled web UI on one port).
`--env-file` loads configuration from a .env file before startup.
`examples` copies the bundled demo workflows somewhere to start from.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

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


@app.command()
def examples(
    dest: str = typer.Argument(
        "./frontflow-examples",
        help="Where to copy the example workflows.",
    ),
) -> None:
    """Copy the bundled example workflows somewhere to start from."""
    dest_dir = Path(dest).expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for src in sorted(_EXAMPLES.glob("*.py")):
        shutil.copy2(src, dest_dir / src.name)
        copied.append(src.name)
    console.print(
        f"[dim]frontflow:[/dim] copied [bold]{len(copied)}[/bold] "
        f"example workflow(s) to {dest_dir}"
    )
    for name in copied:
        console.print(f"  [cyan]{name}[/cyan]")

    # The example Airflow DAGs go in a *sibling* directory — they are
    # Airflow DAG files, not frontflow workflows, so they must not land
    # in the workflows dir where `serve` would try to scan them.
    dag_src = _EXAMPLES / "airflow_dags"
    if dag_src.is_dir():
        dag_dest = dest_dir.parent / "frontflow-example-dags"
        shutil.copytree(dag_src, dag_dest, dirs_exist_ok=True)
        console.print(
            f"\nExample Airflow DAGs copied to {dag_dest}\n"
            f"  [dim](deploy these to your Airflow dags/ folder)[/dim]"
        )

    console.print(
        f"\nServe the workflows with:\n  "
        f"[bold]frontflow serve {dest_dir}[/bold]"
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
) -> None:
    """Create an admin account for the console.

    Run this once to bootstrap — the admin console refuses to serve
    until an account exists. Creating additional admins is allowed; an
    existing username is never overwritten.
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
) -> None:
    """Create a non-admin user account.

    A non-admin sees only the forms in folders a group grant covers.
    Grant access by adding the user to a group in the console's
    Access page.
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
