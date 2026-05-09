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

import subprocess  # noqa: E402
import sys  # noqa: E402
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
    url: str = typer.Argument(..., help="URL to audit (homepage; we crawl from there)."),
    max_pages: int = typer.Option(
        100,
        "--max-pages",
        "-n",
        help="Cap on discovered URLs to crawl. Default 100.",
    ),
) -> None:
    """Run the audit pipeline against a website (multi-page crawl + AI synthesis)."""
    console = Console()
    try:
        run_id, run_dir = asyncio.run(
            run_pipeline(url, _runs_dir(), _db_path(), console, max_pages=max_pages)
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


# Steps the rerun command can re-execute against an existing run, in
# pipeline order. discover/crawl deliberately omitted: their output is the
# foundation everything else reads from, so changing them implies a fresh
# run from scratch.
_RERUN_STEPS_ORDER = [
    "classify",
    "personas",
    "page_aware",
    "page_blind_query_gen",
    "page_blind",
    "verification",
    "persona_reviews",
    "synthesis",
    "html_render",
]
_RERUNNABLE_STEPS = set(_RERUN_STEPS_ORDER)


@app.command()
def rerun(
    run_id: str = typer.Argument(..., help="Full or unique-prefix run_id to reuse."),
    step: str = typer.Argument(
        ...,
        help=(
            "Step to re-run. One of: classify, personas, page_aware, "
            "page_blind_query_gen, page_blind, verification, persona_reviews, "
            "synthesis, html_render."
        ),
    ),
    downstream: bool = typer.Option(
        False,
        "--downstream",
        "-d",
        help="Also re-run every step after the named step, in pipeline order.",
    ),
) -> None:
    """Re-run one step (or a whole tail) against an existing run folder.

    Useful for iterating on prompts: tweak `prompts/04_persona_review.md`,
    then `lens rerun <run_id> persona_reviews` to regenerate only the
    persona reviews + html. With `--downstream`, runs that step and every
    subsequent step in pipeline order — e.g. `lens rerun <run_id> classify
    --downstream` redoes the entire AI pipeline against the existing crawl.

    Pre-AI steps (discover, crawl) are intentionally not re-runnable from
    this command — change those by starting a fresh `lens run`.
    """
    if step not in _RERUNNABLE_STEPS:
        Console().print(
            f"[red]'{step}' is not a re-runnable step.[/] "
            f"Choose one of: {', '.join(_RERUN_STEPS_ORDER)}"
        )
        raise typer.Exit(code=1)

    console = Console()
    manifest = find_run_by_partial_id(_db_path(), run_id)
    if manifest is None:
        console.print(f"[red]no run matching '{run_id}'[/]")
        raise typer.Exit(code=1)

    run_dir = _runs_dir() / manifest.run_id
    fresh = read_manifest(run_dir)

    if downstream:
        idx = _RERUN_STEPS_ORDER.index(step)
        steps_to_run = _RERUN_STEPS_ORDER[idx:]
    else:
        steps_to_run = [step]

    for s in steps_to_run:
        console.print(f"[bold]→ {s}[/]")
        asyncio.run(_rerun_step(s, run_dir, fresh, console))

    console.print(
        f"[bold green]done[/]  {fresh.run_id} :: "
        f"{', '.join(steps_to_run)}  "
        f"[dim]cumulative cost ${fresh.actual_cost_usd:.4f}[/]"
    )


async def _rerun_step(step: str, run_dir: Path, manifest, console: Console) -> None:
    """Execute a single named step + re-render the HTML report."""
    from thelens.config import load_models_config, load_questions
    from thelens.pipeline import classify as classify_step
    from thelens.pipeline import multi_llm as multi_llm_step
    from thelens.pipeline import persona_review as persona_review_step
    from thelens.pipeline import personas as personas_step
    from thelens.pipeline import synthesize as synthesize_step
    from thelens.render.html import render_html
    from thelens.storage import upsert_run, write_manifest

    cfg = load_models_config()
    providers = cfg.enabled_providers()
    synthesis = cfg.synthesis
    questions = load_questions()
    url = manifest.url

    def _track(usage):
        manifest.actual_cost_usd = round(
            manifest.actual_cost_usd + usage.cost_usd, 6
        )
        if usage.provider not in manifest.providers_used:
            manifest.providers_used.append(usage.provider)

    if step == "classify":
        _, usage = await classify_step.classify(run_dir, url)
        _track(usage)

    elif step == "personas":
        personas, usage = await personas_step.generate_personas(run_dir)
        _track(usage)
        manifest.personas_generated = len(personas.personas)

    elif step == "page_aware":
        usages = await multi_llm_step.run_page_aware(
            run_dir, url, providers, questions, console
        )
        for u in usages:
            _track(u)

    elif step == "page_blind_query_gen":
        from thelens.models import Classification
        cls = Classification.model_validate_json(
            (run_dir / "classification.json").read_text(encoding="utf-8")
        )
        _, usage = await multi_llm_step.run_page_blind_query_generation(
            run_dir, cls, synthesis
        )
        _track(usage)

    elif step == "page_blind":
        from thelens.models import PageBlindQuerySet
        qs = PageBlindQuerySet.model_validate_json(
            (run_dir / "page_blind_queries.json").read_text(encoding="utf-8")
        )
        usages = await multi_llm_step.run_page_blind(
            run_dir, url, qs, providers, console
        )
        for u in usages:
            _track(u)

    elif step == "verification":
        usages = await multi_llm_step.run_verification(
            run_dir, url, providers, synthesis, console
        )
        for u in usages:
            _track(u)

    elif step == "persona_reviews":
        usages = await persona_review_step.run_persona_reviews(
            run_dir, url, synthesis, console
        )
        for u in usages:
            _track(u)

    elif step == "synthesis":
        result, usage = await synthesize_step.run_synthesis(
            run_dir, url, providers, synthesis
        )
        _track(usage)
        manifest.composite_score = result.composite_score

    # Always re-render the report after a step finishes successfully so
    # the user sees the new output immediately.
    render_html(run_dir, manifest)
    manifest.step_status[step] = "complete"  # type: ignore[assignment]
    if step != "html_render":
        manifest.step_status["html_render"] = "complete"  # type: ignore[assignment]
    write_manifest(run_dir, manifest)
    upsert_run(_db_path(), manifest)


@app.command()
def reindex() -> None:
    """Rebuild the SQLite index from the runs/ folder."""
    console = Console()
    count = reindex_from_filesystem(_db_path(), _runs_dir())
    console.print(f"reindexed {count} run{'s' if count != 1 else ''}")


@app.command()
def dashboard(
    port: int = typer.Option(8501, "--port", "-p", help="Port for Streamlit."),
) -> None:
    """Launch the Streamlit web UI."""
    app_path = Path(__file__).parent / "app.py"
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.port",
        str(port),
        "--server.headless",
        "false",
    ]
    subprocess.run(cmd, check=False)


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)
