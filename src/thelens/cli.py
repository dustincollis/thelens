"""Typer CLI entry point. Wires `run`, `list`, `reindex`, and `version`."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console

# Load .env before any imports that read API keys at module level.
# `override=True` because some host environments (Claude Desktop, certain
# IDEs) inject empty `*_API_KEY=` into the env, which would otherwise win.
load_dotenv(override=True)

import webbrowser  # noqa: E402

from thelens import __version__  # noqa: E402
from thelens.pipeline.run import run_pipeline  # noqa: E402
from thelens.render.html import render_html  # noqa: E402
from thelens.storage import (  # noqa: E402
    find_run_by_partial_id,
    list_recent_runs,
    read_manifest,
    reindex_from_filesystem,
)


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
def open(  # noqa: A001 — `open` shadows builtin only inside this command callable
    run_id: str = typer.Argument(..., help="Full or unique-prefix run_id."),
    rerender: bool = typer.Option(
        False, "--rerender", help="Re-render report.html before opening."
    ),
) -> None:
    """Open a run's HTML report in the default browser."""
    console = Console()
    manifest = find_run_by_partial_id(_db_path(), run_id)
    if manifest is None:
        console.print(f"[red]no run matching '{run_id}'[/]")
        raise typer.Exit(code=1)

    run_dir = _runs_dir() / manifest.run_id
    report_path = run_dir / "report.html"

    if rerender or not report_path.exists():
        # Re-load fresh manifest from disk before rendering, in case the
        # SQLite index is stale.
        fresh = read_manifest(run_dir)
        report_path = render_html(run_dir, fresh)
        console.print(f"[dim]rendered[/] {report_path}")

    webbrowser.open(report_path.as_uri())
    console.print(f"[green]opened[/] {report_path}")


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
