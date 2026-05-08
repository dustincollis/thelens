"""Pipeline orchestrator.

Phase 1: fetch + audit.
Phase 2: classify + personas.
Phase 3: page_aware + page_blind (with query gen) + verification.
"""

from __future__ import annotations

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
from thelens.pipeline import audit as audit_step
from thelens.pipeline import classify as classify_step
from thelens.pipeline import fetch as fetch_step
from thelens.pipeline import multi_llm as multi_llm_step
from thelens.pipeline import personas as personas_step
from thelens.storage import (
    create_run_folder,
    init_db,
    make_run_id,
    upsert_run,
    write_manifest,
)


def _initial_step_status() -> dict[str, str]:
    return {
        "fetch": "pending",
        "audit": "pending",
        "classify": "pending",
        "personas": "pending",
        "page_aware": "pending",
        "page_blind_query_gen": "pending",
        "page_blind": "pending",
        "verification": "pending",
    }


async def run_pipeline(
    url: str,
    runs_dir: Path,
    db_path: Path,
    console: Console | None = None,
) -> tuple[str, Path]:
    """Execute the pipeline end-to-end. Returns `(run_id, run_dir)`."""
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
        f"[dim]({len(questions)} questions)[/]"
    )

    classification: Classification | None = None
    queries: PageBlindQuerySet | None = None

    try:
        await _run_step(
            "fetch",
            lambda: fetch_step.fetch_all(url, run_dir),
            manifest,
            run_dir,
            db_path,
            console,
        )

        async def _do_audit() -> None:
            audit_result = await audit_step.audit_url(url, run_dir)
            (run_dir / "technical_audit.json").write_text(
                audit_result.model_dump_json(indent=2),
                encoding="utf-8",
            )

        await _run_step("audit", _do_audit, manifest, run_dir, db_path, console)

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
            manifest,
            run_dir,
            db_path,
            console,
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

        manifest.status = "complete"
        manifest.completed_at = datetime.now(timezone.utc)
    except Exception:
        manifest.status = "failed"
        manifest.completed_at = datetime.now(timezone.utc)
        _persist(manifest, run_dir, db_path)
        raise

    _persist(manifest, run_dir, db_path)
    console.print(
        f"[dim]cost     [/] ${manifest.actual_cost_usd:.4f}  "
        f"[dim]personas [/] {manifest.personas_generated}"
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
    if name in {"fetch", "audit", "classify", "personas", "page_blind_query_gen"}:
        console.print("[green]ok[/]")
    else:
        # Multi-provider steps print their own per-provider lines; this step's
        # newline came from those.
        console.print("  [green]done[/]")


def _record_usage(manifest: RunManifest, usage: UsageInfo) -> None:
    manifest.actual_cost_usd = round(manifest.actual_cost_usd + usage.cost_usd, 6)
    if usage.provider not in manifest.providers_used:
        manifest.providers_used.append(usage.provider)


def _persist(manifest: RunManifest, run_dir: Path, db_path: Path) -> None:
    write_manifest(run_dir, manifest)
    upsert_run(db_path, manifest)
