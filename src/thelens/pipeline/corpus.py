"""Site corpus + audit aggregation for use in AI prompts.

The corpus is a labeled concatenation of cleaned text from every
crawled page, ordered homepage → anchors → other pages. Per-page text
is capped to keep total tokens within Opus's context window.

The audit summary aggregates per-page TechnicalAudit JSON into a
compact site-level signal: homepage's full audit + cross-page stats.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from thelens.pipeline._extract import extract_title, extract_visible_text


# Per-page cap: ~5k chars ≈ 1.25k tokens. 100 pages → 500k chars / ~125k tokens.
# Most marketing pages have less than this in cleaned text anyway.
_PER_PAGE_CHARS = 5000

# Total corpus cap: safety belt. Opus context is 200k tokens; we leave
# ~50k headroom for system prompt + other inputs + output budget.
_TOTAL_CHARS = 600_000


def build_site_corpus(run_dir: Path) -> str:
    """Concatenate cleaned text from every successfully-crawled page.

    Format per page:
        ## URL: https://example.com/services
        Title: Services

        <up to 5000 chars of cleaned text>

        ---

    Pages are ordered: homepage first, then anchor pages by URL, then
    depth-2 pages by URL. Failed-to-crawl pages are skipped.
    """
    discovery_path = run_dir / "discovery.json"
    if not discovery_path.exists():
        return ""
    discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
    pages_dir = run_dir / "pages"

    sorted_pages = sorted(
        (p for p in discovery["pages"] if p["status"] == "complete"),
        key=lambda p: (p["depth"], p["url"]),
    )

    parts: list[str] = []
    total = 0
    for page in sorted_pages:
        rendered = pages_dir / page["slug"] / "rendered_dom.html"
        if not rendered.exists():
            continue
        html = rendered.read_text(encoding="utf-8")
        title = extract_title(html)
        text = extract_visible_text(html)[:_PER_PAGE_CHARS]
        if not text.strip():
            continue

        section = (
            f"## URL: {page['url']}\n"
            f"Title: {title}\n\n"
            f"{text}\n\n"
            f"---\n"
        )
        if total + len(section) > _TOTAL_CHARS:
            break
        parts.append(section)
        total += len(section)
    return "\n".join(parts)


def homepage_record(run_dir: Path) -> dict[str, Any] | None:
    discovery_path = run_dir / "discovery.json"
    if not discovery_path.exists():
        return None
    discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
    for p in discovery["pages"]:
        if p["depth"] == 0:
            return p
    return discovery["pages"][0] if discovery["pages"] else None


def homepage_title(run_dir: Path) -> str:
    rec = homepage_record(run_dir)
    if not rec:
        return ""
    rendered = run_dir / "pages" / rec["slug"] / "rendered_dom.html"
    if not rendered.exists():
        return ""
    return extract_title(rendered.read_text(encoding="utf-8"))


def homepage_url(run_dir: Path) -> str:
    rec = homepage_record(run_dir)
    return rec["url"] if rec else ""


def build_audit_summary(run_dir: Path) -> dict[str, Any]:
    """Aggregate per-page audits into a compact site-level summary.

    Returns the homepage's full audit alongside cross-page stats so the
    synthesis prompt can reason about both site-wide patterns and the
    homepage specifically.
    """
    discovery_path = run_dir / "discovery.json"
    if not discovery_path.exists():
        return {}
    discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
    pages_dir = run_dir / "pages"

    homepage_audit: dict[str, Any] | None = None
    js_trapped_values: list[float] = []
    h1_violation_pages = 0
    no_privacy_pages = 0
    no_contact_pages = 0
    no_https_pages = 0
    alt_coverage_values: list[float] = []
    pages_with_jsonld = 0
    audited = 0

    for page in discovery["pages"]:
        if page["status"] != "complete":
            continue
        audit_path = pages_dir / page["slug"] / "technical_audit.json"
        if not audit_path.exists():
            continue
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        audited += 1
        if page["depth"] == 0:
            homepage_audit = audit

        js_trapped_values.append(audit["render_mode_diff"]["js_trapped_pct"])
        if audit["html_structure"]["heading_hierarchy_violations"] > 0:
            h1_violation_pages += 1
        if not audit["trust_signals"]["privacy_policy_link"]:
            no_privacy_pages += 1
        if not audit["trust_signals"]["contact_info_present"]:
            no_contact_pages += 1
        if not audit["trust_signals"]["https"]:
            no_https_pages += 1
        alt_coverage_values.append(audit["html_structure"]["alt_text_coverage_pct"])
        if audit["structured_data"]["json_ld_blocks"] > 0:
            pages_with_jsonld += 1

    def _avg(xs: list[float]) -> float:
        return round(sum(xs) / len(xs), 1) if xs else 0.0

    return {
        "pages_audited": audited,
        "homepage": homepage_audit,
        "site_aggregates": {
            "avg_js_trapped_pct": _avg(js_trapped_values),
            "pages_with_heading_hierarchy_violations": h1_violation_pages,
            "pages_missing_privacy_link": no_privacy_pages,
            "pages_missing_contact_info": no_contact_pages,
            "pages_not_https": no_https_pages,
            "avg_alt_text_coverage_pct": _avg(alt_coverage_values),
            "pages_with_json_ld": pages_with_jsonld,
        },
    }
