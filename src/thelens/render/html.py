"""Step 9: render the HTML report from a completed run folder.

Reads every JSON artifact in `runs/<run_id>/`, hands them to the Jinja
template at `templates/report.html.j2`, and writes `report.html` next to
the screenshots so relative `<img>` paths work. Auto-escape is on — any
LLM-generated text that happens to contain HTML is escaped, not rendered.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt
from markupsafe import Markup

from thelens.config import load_questions, project_root
from thelens.models import RunManifest
from thelens.pipeline.corpus import build_audit_summary, homepage_record


_ICON_PATH = Path(__file__).resolve().parent.parent / "assets" / "lens_icon.svg"


# CommonMark renderer with HTML disabled (LLM output should not be able to
# inject raw <script> or <iframe> tags). Linkify auto-detects bare URLs.
_MD = MarkdownIt("default", {"html": False, "linkify": False, "breaks": False})


def _md_block(text: str | None) -> Markup:
    """Render Markdown text with block-level wrapping (paragraphs, lists)."""
    if not text:
        return Markup("")
    return Markup(_MD.render(text))


def _md_inline(text: str | None) -> Markup:
    """Render Markdown without wrapping in <p>; for use inside existing block elements."""
    if not text:
        return Markup("")
    return Markup(_MD.renderInline(text))


def render_html(run_dir: Path, manifest: RunManifest) -> Path:
    """Render `report.html` into `run_dir`. Returns the path."""
    artifacts = _load_artifacts(run_dir)
    env = _build_env()
    template = env.get_template("report.html.j2")
    icon_svg, icon_data_uri = _load_icon()
    html = template.render(
        manifest=manifest.model_dump(mode="json"),
        rendered_at=datetime.now(timezone.utc).isoformat(),
        questions=[q.model_dump() for q in load_questions()],
        icon_svg=icon_svg,
        icon_data_uri=icon_data_uri,
        **artifacts,
    )
    target = run_dir / "report.html"
    target.write_text(html, encoding="utf-8")
    return target


def _load_icon() -> tuple[Markup, str]:
    """Return `(inline_svg, favicon_data_uri)` for the bundled lens icon.

    Inline SVG is wrapped in `Markup` so Jinja autoescape leaves it alone
    when embedded in the header. Favicon is base64 since data-URI utf-8
    encoding for SVG is finicky across browsers.
    """
    if not _ICON_PATH.exists():
        return Markup(""), ""
    raw = _ICON_PATH.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    return Markup(raw.decode("utf-8")), f"data:image/svg+xml;base64,{b64}"


def _build_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(project_root() / "templates"),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["md"] = _md_block
    env.filters["mdinline"] = _md_inline
    return env


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
            # Skip failed-marker JSONs — they don't have the review fields
            # the template expects.
            if data and data.get("status") != "failed":
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
