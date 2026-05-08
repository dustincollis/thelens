"""Pipeline orchestrator.

Phase 1 wired fetch + audit. Phase 2 adds classify + personas.
Each step extends `_initial_step_status()` and runs in the try block.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from rich.console import Console

from thelens.models import RunManifest, UsageInfo
from thelens.pipeline import audit as audit_step
from thelens.pipeline import classify as classify_step
from thelens.pipeline import fetch as fetch_step
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
    }


async def run_pipeline(
    url: str,
    runs_dir: Path,
    db_path: Path,
    console: Console | None = None,
) -> tuple[str, Path]:
    """Execute the pipeline end-to-end. Returns `(run_id, run_dir)`.

    Phase 2 scope: fetch, audit, classify, personas. The manifest is rewritten
    after every step so a crash mid-run leaves the on-disk state recoverable.
    """
    console = console or Console()
    init_db(db_path)
    runs_dir.mkdir(parents=True, exist_ok=True)

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

    console.print(f"[dim]run_id [/] {run_id}")
    console.print(f"[dim]folder [/] {run_dir}")

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
            _, usage = await classify_step.classify(run_dir, url)
            _record_usage(manifest, usage)

        await _run_step("classify", _do_classify, manifest, run_dir, db_path, console)

        async def _do_personas() -> None:
            personas, usage = await personas_step.generate_personas(run_dir)
            _record_usage(manifest, usage)
            manifest.personas_generated = len(personas.personas)

        await _run_step("personas", _do_personas, manifest, run_dir, db_path, console)

        manifest.status = "complete"
        manifest.completed_at = datetime.now(timezone.utc)
    except Exception:
        manifest.status = "failed"
        manifest.completed_at = datetime.now(timezone.utc)
        _persist(manifest, run_dir, db_path)
        raise

    _persist(manifest, run_dir, db_path)
    console.print(
        f"[dim]cost   [/] ${manifest.actual_cost_usd:.4f}  "
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
    console.print("[green]ok[/]")


def _record_usage(manifest: RunManifest, usage: UsageInfo) -> None:
    manifest.actual_cost_usd = round(manifest.actual_cost_usd + usage.cost_usd, 6)
    if usage.provider not in manifest.providers_used:
        manifest.providers_used.append(usage.provider)


def _persist(manifest: RunManifest, run_dir: Path, db_path: Path) -> None:
    write_manifest(run_dir, manifest)
    upsert_run(db_path, manifest)
