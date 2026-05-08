"""Typer CLI entry point. Real commands are wired up in later phases."""

import typer

from thelens import __version__

app = typer.Typer(
    name="lens",
    help="The Lens — local-first website audit tool.",
    no_args_is_help=True,
)


@app.callback()
def _root() -> None:
    """Multi-command CLI; subcommands are registered below."""


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)
