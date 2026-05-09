"""Pipeline orchestrator (multi-page).

Step list:
  discover         enumerate up to N URLs from the homepage
  crawl            fetch + audit every discovered page in parallel
  classify         site fingerprint (Layer 1)
  personas         3-5 review personas (Layer 2)
  page_aware       site-aware structured Q&A per provider (Layer 3a)
  page_blind_*     category-level brand visibility (Layer 3b)
  verification     fact-check page-aware against site corpus
  persona_reviews  per-persona site review with prompt caching (Layer 4)
  synthesis        cross-lens synthesis with composite score (Layer 5)
  html_render      browser-readable report
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from rich.console import Console

from thelens.config import load_models_config, load_questions
from thelens.models import (
    Classification,
    PageBlindQuerySet,
    RunManifest,
    UsageInfo,
)
from thelens.pipeline import classify as classify_step
from thelens.pipeline import crawl as crawl_step
from thelens.pipeline import discover as discover_step
from thelens.pipeline import multi_llm as multi_llm_step
from thelens.pipeline import persona_review as persona_review_step
from thelens.pipeline import personas as personas_step
from thelens.pipeline import synthesize as synthesize_step
from thelens.render.html import render_html
from thelens.storage import (
    create_run_folder,
    init_db,
    make_run_id,
    upsert_run,
    write_manifest,
)


_DEFAULT_MAX_PAGES = 100


def _initial_step_status() -> dict[str, str]:
    return {
        "discover": "pending",
        "crawl": "pending",
        "classify": "pending",
        "personas": "pending",
        "page_aware": "pending",
        "page_blind_query_gen": "pending",
        "page_blind": "pending",
        "verification": "pending",
        "persona_reviews": "pending",
        "synthesis": "pending",
        "html_render": "pending",
    }


async def run_pipeline(
    url: str,
    runs_dir: Path,
    db_path: Path,
    console: Console | None = None,
    max_pages: int = _DEFAULT_MAX_PAGES,
) -> tuple[str, Path]:
    """Run the multi-page audit pipeline. Returns `(run_id, run_dir)`."""
    console = console or Console()
    init_db(db_path)
    runs_dir.mkdir(parents=True, exist_ok=True)

    models_config = load_models_config()
    providers = models_config.enabled_providers()
    synthesis = models_config.synthesis
    questions = load_questions()

    if not providers:
        raise RuntimeError(
            "No providers are enabled in config/models.yaml. "
            "Set at least one provider's `enabled: true`."
        )

    started = datetime.now(timezone.utc)
    run_id = make_run_id(url, started)
    run_dir = create_run_folder(run_id, runs_dir)

    manifest = RunManifest(
        run_id=run_id,
        url=url,
        started_at=started,
        status="running",
        step_status=_initial_step_status(),  # type: ignore[arg-type]
    )
    _persist(manifest, run_dir, db_path)

    console.print(f"[dim]run_id   [/] {run_id}")
    console.print(f"[dim]folder   [/] {run_dir}")
    console.print(
        f"[dim]providers[/] {', '.join(p.name for p in providers)} "
        f"[dim]({len(questions)} questions, max {max_pages} pages)[/]"
    )

    discovered: list = []
    classification: Classification | None = None
    queries: PageBlindQuerySet | None = None

    try:
        async def _do_discover() -> None:
            nonlocal discovered
            discovered = await discover_step.discover(url, max_pages=max_pages)
            console.print(
                f"    discovered {len(discovered)} pages "
                f"[dim]({sum(1 for p in discovered if p.is_anchor)} anchors)[/]"
            )

        await _run_step(
            "discover", _do_discover, manifest, run_dir, db_path, console
        )

        async def _do_crawl() -> None:
            await crawl_step.crawl_pages(discovered, run_dir, console)

        await _run_step("crawl", _do_crawl, manifest, run_dir, db_path, console)

        async def _do_classify() -> None:
            nonlocal classification
            parsed, usage = await classify_step.classify(run_dir, url)
            classification = parsed
            _record_usage(manifest, usage)

        await _run_step("classify", _do_classify, manifest, run_dir, db_path, console)

        async def _do_personas() -> None:
            personas, usage = await personas_step.generate_personas(run_dir)
            _record_usage(manifest, usage)
            manifest.personas_generated = len(personas.personas)

        await _run_step("personas", _do_personas, manifest, run_dir, db_path, console)

        async def _do_page_aware() -> None:
            usages = await multi_llm_step.run_page_aware(
                run_dir, url, providers, questions, console
            )
            for u in usages:
                _record_usage(manifest, u)

        await _run_step(
            "page_aware", _do_page_aware, manifest, run_dir, db_path, console
        )

        async def _do_page_blind_query_gen() -> None:
            nonlocal queries
            assert classification is not None
            qs, usage = await multi_llm_step.run_page_blind_query_generation(
                run_dir, classification, synthesis
            )
            queries = qs
            _record_usage(manifest, usage)

        await _run_step(
            "page_blind_query_gen",
            _do_page_blind_query_gen,
            manifest, run_dir, db_path, console,
        )

        async def _do_page_blind() -> None:
            assert queries is not None
            usages = await multi_llm_step.run_page_blind(
                run_dir, url, queries, providers, console
            )
            for u in usages:
                _record_usage(manifest, u)

        await _run_step(
            "page_blind", _do_page_blind, manifest, run_dir, db_path, console
        )

        async def _do_verification() -> None:
            usages = await multi_llm_step.run_verification(
                run_dir, url, providers, synthesis, console
            )
            for u in usages:
                _record_usage(manifest, u)

        await _run_step(
            "verification", _do_verification, manifest, run_dir, db_path, console
        )

        async def _do_persona_reviews() -> None:
            usages = await persona_review_step.run_persona_reviews(
                run_dir, url, synthesis, console
            )
            for u in usages:
                _record_usage(manifest, u)

        await _run_step(
            "persona_reviews",
            _do_persona_reviews,
            manifest, run_dir, db_path, console,
        )

        async def _do_synthesis() -> None:
            result, usage = await synthesize_step.run_synthesis(
                run_dir, url, providers, synthesis
            )
            _record_usage(manifest, usage)
            manifest.composite_score = result.composite_score

        await _run_step(
            "synthesis", _do_synthesis, manifest, run_dir, db_path, console
        )

        async def _do_html_render() -> None:
            render_html(run_dir, manifest)

        await _run_step(
            "html_render", _do_html_render, manifest, run_dir, db_path, console
        )

        manifest.status = "complete"
        manifest.completed_at = datetime.now(timezone.utc)
    except Exception:
        manifest.status = "failed"
        manifest.completed_at = datetime.now(timezone.utc)
        _persist(manifest, run_dir, db_path)
        raise

    _persist(manifest, run_dir, db_path)
    score = (
        f"{manifest.composite_score}/100"
        if manifest.composite_score is not None
        else "n/a"
    )
    console.print(
        f"[dim]cost     [/] ${manifest.actual_cost_usd:.4f}  "
        f"[dim]personas [/] {manifest.personas_generated}  "
        f"[dim]score    [/] {score}  "
        f"[dim]pages    [/] {len(discovered)}"
    )
    return run_id, run_dir


async def _run_step(
    name: str,
    fn: Callable[[], Awaitable[None]],
    manifest: RunManifest,
    run_dir: Path,
    db_path: Path,
    console: Console,
) -> None:
    console.print(f"  {name}... ", end="")
    manifest.step_status[name] = "running"  # type: ignore[assignment]
    _persist(manifest, run_dir, db_path)
    try:
        await fn()
    except Exception as exc:
        manifest.step_status[name] = "failed"  # type: ignore[assignment]
        _persist(manifest, run_dir, db_path)
        console.print(f"[red]failed[/]: {exc}")
        raise
    manifest.step_status[name] = "complete"  # type: ignore[assignment]
    _persist(manifest, run_dir, db_path)
    if name in {
        "classify",
        "personas",
        "page_blind_query_gen",
        "synthesis",
        "html_render",
    }:
        console.print("[green]ok[/]")
    else:
        # Multi-step phases print their own per-substep lines.
        console.print("  [green]done[/]")


def _record_usage(manifest: RunManifest, usage: UsageInfo) -> None:
    manifest.actual_cost_usd = round(manifest.actual_cost_usd + usage.cost_usd, 6)
    if usage.provider not in manifest.providers_used:
        manifest.providers_used.append(usage.provider)


def _persist(manifest: RunManifest, run_dir: Path, db_path: Path) -> None:
    write_manifest(run_dir, manifest)
    upsert_run(db_path, manifest)
