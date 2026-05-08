"""Typer CLI entry point. Phase 1 wires `run`, `list`, `reindex`, and `version`."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console

from thelens import __version__
from thelens.pipeline.run import run_pipeline
from thelens.storage import list_recent_runs, reindex_from_filesystem


app = typer.Typer(
    name="lens",
    help="The Lens — local-first website audit tool.",
    no_args_is_help=True,
)


def _project_root() -> Path:
    """Project root = current working directory.

    `uv run lens ...` invokes us from the project root by default. If the
    user runs from elsewhere, `runs/` and `data/` resolve relative to CWD.
    """
    return Path.cwd()


def _runs_dir() -> Path:
    return _project_root() / "runs"


def _db_path() -> Path:
    return _project_root() / "data" / "runs.db"


@app.callback()
def _root() -> None:
    """Multi-command CLI; subcommands are registered below."""


@app.command()
def run(
    url: str = typer.Argument(..., help="URL to audit."),
) -> None:
    """Run the audit pipeline for a single URL."""
    console = Console()
    try:
        run_id, run_dir = asyncio.run(
            run_pipeline(url, _runs_dir(), _db_path(), console)
        )
    except Exception as exc:
        console.print(f"[bold red]run failed:[/] {exc}")
        raise typer.Exit(code=1)
    console.print(f"[bold green]done[/]  {run_id}")
    console.print(f"  {run_dir}")


@app.command("list")
def list_cmd(
    limit: int = typer.Option(20, "--limit", "-n", help="Max runs to show."),
) -> None:
    """Show recent runs, most recent first."""
    console = Console()
    runs = list_recent_runs(_db_path(), limit=limit)
    if not runs:
        console.print("[dim]no runs found[/]")
        return
    for r in runs:
        color = {
            "complete": "green",
            "failed": "red",
            "running": "yellow",
            "pending": "dim",
        }.get(r.status, "white")
        console.print(f"[{color}]{r.status:>9}[/]  {r.run_id}  [dim]{r.url}[/]")


@app.command()
def reindex() -> None:
    """Rebuild the SQLite index from the runs/ folder."""
    console = Console()
    count = reindex_from_filesystem(_db_path(), _runs_dir())
    console.print(f"reindexed {count} run{'s' if count != 1 else ''}")


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)
