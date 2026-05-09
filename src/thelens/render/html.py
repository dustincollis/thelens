"""Step 9: render the HTML report from a completed run folder.

Reads every JSON artifact in `runs/<run_id>/`, hands them to the Jinja
template at `templates/report.html.j2`, and writes `report.html` next to
the screenshots so relative `<img>` paths work. Auto-escape is on — any
LLM-generated text that happens to contain HTML is escaped, not rendered.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from thelens.config import load_questions, project_root
from thelens.models import RunManifest
from thelens.pipeline.corpus import build_audit_summary, homepage_record


def render_html(run_dir: Path, manifest: RunManifest) -> Path:
    """Render `report.html` into `run_dir`. Returns the path."""
    artifacts = _load_artifacts(run_dir)
    env = _build_env()
    template = env.get_template("report.html.j2")
    html = template.render(
        manifest=manifest.model_dump(mode="json"),
        rendered_at=datetime.now(timezone.utc).isoformat(),
        questions=[q.model_dump() for q in load_questions()],
        **artifacts,
    )
    target = run_dir / "report.html"
    target.write_text(html, encoding="utf-8")
    return target


def _build_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(project_root() / "templates"),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _load_artifacts(run_dir: Path) -> dict[str, object]:
    """Load every JSON artifact the template might need."""
    out: dict[str, object] = {
        "audit": build_audit_summary(run_dir),
        "classification": _read_json(run_dir / "classification.json"),
        "personas": _read_json(run_dir / "personas.json"),
        "synthesis": _read_json(run_dir / "synthesis.json"),
        "page_blind_queries": _read_json(run_dir / "page_blind_queries.json"),
        "discovery": _read_json(run_dir / "discovery.json"),
        "homepage": homepage_record(run_dir),
    }

    page_aware: dict[str, dict] = {}
    page_blind: dict[str, dict] = {}
    llm_dir = run_dir / "llm"
    if llm_dir.exists():
        for f in sorted(llm_dir.glob("*_page_aware.json")):
            data = _read_json(f)
            if data and data.get("status") != "failed":
                provider = f.stem[: -len("_page_aware")]
                page_aware[provider] = data
        for f in sorted(llm_dir.glob("*_page_blind.json")):
            data = _read_json(f)
            if data and data.get("status") != "failed":
                provider = f.stem[: -len("_page_blind")]
                page_blind[provider] = data
    out["page_aware"] = page_aware
    out["page_blind"] = page_blind

    # Load persona reviews and join with personas.json so the report can
    # show "why this persona was generated" alongside their review output.
    persona_reviews: list[dict] = []
    review_dir = run_dir / "persona_reviews"
    if review_dir.exists():
        for f in sorted(review_dir.glob("persona_*.json")):
            data = _read_json(f)
            if data:
                persona_reviews.append(data)

    personas_data = out.get("personas")
    if isinstance(personas_data, dict):
        by_name = {p.get("name"): p for p in personas_data.get("personas", [])}
        for review in persona_reviews:
            persona = by_name.get(review.get("persona_name"))
            if persona:
                review["persona_context"] = persona.get("context", "")
                review["persona_goal"] = persona.get("goal", "")
                review["persona_rationale"] = persona.get("rationale", "")
                review["persona_primary_concerns"] = persona.get("primary_concerns", [])
                review["persona_is_llm_lens"] = persona.get("is_llm_lens", False)
    out["persona_reviews"] = persona_reviews

    return out


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
