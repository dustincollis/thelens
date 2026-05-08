"""Filesystem and SQLite storage for The Lens.

The filesystem is the source of truth: every per-run artifact lives under
`runs/<run_id>/`. SQLite (`data/runs.db`) is a convenience index, mirroring
flat fields from each `RunManifest` so history queries and the dashboard
don't have to scan the filesystem. SQLite is fully rebuildable via
`reindex_from_filesystem()`.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from thelens.models import RunManifest


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    url                 TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    completed_at        TEXT,
    status              TEXT NOT NULL,
    providers_used      TEXT NOT NULL,
    personas_generated  INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd  REAL NOT NULL DEFAULT 0,
    actual_cost_usd     REAL NOT NULL DEFAULT 0,
    composite_score     INTEGER,
    step_status         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at DESC);
"""


def _sanitize_domain(url: str) -> str:
    """Domain-from-URL transform spec'd in CLAUDE.md (stable across runs)."""
    return urlparse(url).netloc.replace(".", "-").replace(":", "-")


def make_run_id(url: str, now: datetime) -> str:
    """Build the immutable run_id: YYYY-MM-DD_<sanitized-domain>_<6-char-hex>.

    The hex suffix is `secrets.token_hex(3)`, NOT a hash of the URL — two runs
    of the same URL get distinct IDs.
    """
    date = now.strftime("%Y-%m-%d")
    domain = _sanitize_domain(url) or "unknown"
    suffix = secrets.token_hex(3)
    return f"{date}_{domain}_{suffix}"


def create_run_folder(run_id: str, runs_dir: Path) -> Path:
    """Create the run folder and its `llm/` and `persona_reviews/` subfolders."""
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "llm").mkdir()
    (run_dir / "persona_reviews").mkdir()
    return run_dir


def write_manifest(run_dir: Path, manifest: RunManifest) -> None:
    path = run_dir / "manifest.json"
    path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")


def read_manifest(run_dir: Path) -> RunManifest:
    path = run_dir / "manifest.json"
    return RunManifest.model_validate_json(path.read_text(encoding="utf-8"))


def init_db(db_path: Path) -> None:
    """Create the SQLite schema if missing. Enables WAL for safer concurrent reads."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)


def _row_from_manifest(m: RunManifest) -> tuple:
    return (
        m.run_id,
        m.url,
        m.started_at.isoformat(),
        m.completed_at.isoformat() if m.completed_at else None,
        m.status,
        json.dumps(m.providers_used),
        m.personas_generated,
        m.estimated_cost_usd,
        m.actual_cost_usd,
        m.composite_score,
        json.dumps(m.step_status),
    )


def upsert_run(db_path: Path, manifest: RunManifest) -> None:
    init_db(db_path)
    row = _row_from_manifest(manifest)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO runs (
                run_id, url, started_at, completed_at, status,
                providers_used, personas_generated, estimated_cost_usd,
                actual_cost_usd, composite_score, step_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                url                 = excluded.url,
                started_at          = excluded.started_at,
                completed_at        = excluded.completed_at,
                status              = excluded.status,
                providers_used      = excluded.providers_used,
                personas_generated  = excluded.personas_generated,
                estimated_cost_usd  = excluded.estimated_cost_usd,
                actual_cost_usd     = excluded.actual_cost_usd,
                composite_score     = excluded.composite_score,
                step_status         = excluded.step_status
            """,
            row,
        )
        conn.commit()


def list_recent_runs(db_path: Path, limit: int = 20) -> list[RunManifest]:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_manifest_from_row(r) for r in rows]


def _manifest_from_row(row: sqlite3.Row) -> RunManifest:
    return RunManifest(
        run_id=row["run_id"],
        url=row["url"],
        started_at=datetime.fromisoformat(row["started_at"]),
        completed_at=(
            datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
        ),
        status=row["status"],
        providers_used=json.loads(row["providers_used"]),
        personas_generated=row["personas_generated"],
        estimated_cost_usd=row["estimated_cost_usd"],
        actual_cost_usd=row["actual_cost_usd"],
        composite_score=row["composite_score"],
        step_status=json.loads(row["step_status"]),
    )


def find_run_by_partial_id(db_path: Path, partial: str) -> RunManifest | None:
    """Look up a run by full or unique-prefix run_id."""
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM runs WHERE run_id LIKE ? ORDER BY started_at DESC",
            (f"{partial}%",),
        ).fetchall()
    if not rows:
        return None
    return _manifest_from_row(rows[0])


def reindex_from_filesystem(db_path: Path, runs_dir: Path) -> int:
    """Drop and rebuild the SQLite index from `runs/*/manifest.json`.

    Returns the number of runs indexed. Run folders without a valid manifest
    are skipped (a warning is the caller's responsibility).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE IF EXISTS runs")
        conn.executescript(_SCHEMA)
    count = 0
    if not runs_dir.exists():
        return 0
    for entry in sorted(runs_dir.iterdir()):
        if not entry.is_dir():
            continue
        manifest_path = entry / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = read_manifest(entry)
        except Exception:
            continue
        upsert_run(db_path, manifest)
        count += 1
    return count
