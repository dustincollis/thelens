"""Step 2: pure-Python technical and structural audit.

No AI calls. Reads the artifacts written by `fetch.py` plus the live
robots.txt / llms.txt for the URL's domain, and produces a `TechnicalAudit`
written to `technical_audit.json`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup, Tag

from thelens.models import (
    AiCrawlerAccess,
    HtmlStructure,
    LlmsTxt,
    PageSize,
    RenderModeDiff,
    SemanticTagUsage,
    StructuredData,
    TechnicalAudit,
    TrustSignals,
)


KNOWN_AI_CRAWLERS = [
    "GPTBot",
    "ClaudeBot",
    "anthropic-ai",
    "Google-Extended",
    "PerplexityBot",
    "CCBot",
    "Bytespider",
    "Applebot-Extended",
]

RECOMMENDED_SCHEMAS = ["Organization", "WebSite", "BreadcrumbList"]

LOW_QUALITY_LINK_TEXTS = {
    "click here",
    "here",
    "link",
    "more",
    "read more",
    "learn more",
    "this",
    "this link",
}

_HTTP_TIMEOUT_S = 15.0
_USER_AGENT = "TheLens/0.1 (audit; +https://github.com)"


# ============================================================================
# Top-level entry point
# ============================================================================


async def audit_url(url: str, run_dir: Path) -> TechnicalAudit:
    raw_html = (run_dir / "raw_html.html").read_text(encoding="utf-8")
    rendered_html = (run_dir / "rendered_dom.html").read_text(encoding="utf-8")

    raw_text = _extract_visible_text(raw_html)
    rendered_text = _extract_visible_text(rendered_html)
    soup = BeautifulSoup(rendered_html, "lxml")

    robots_text, robots_present = await _fetch_robots(url)
    llms_text, llms_present = await _fetch_llms(url)

    return TechnicalAudit(
        url=url,
        fetched_at=datetime.now(timezone.utc),
        render_mode_diff=_render_mode_diff(raw_text, rendered_text),
        html_structure=_html_structure(soup),
        structured_data=_structured_data(soup),
        ai_crawler_access=_ai_crawler_access(robots_text, robots_present),
        llms_txt=_llms_txt(llms_text, llms_present),
        trust_signals=_trust_signals(url, soup),
        page_size=_page_size(raw_html, rendered_html),
    )


# ============================================================================
# Render-mode diff
# ============================================================================


def _render_mode_diff(raw_text: str, rendered_text: str) -> RenderModeDiff:
    raw_n = len(raw_text)
    rendered_n = len(rendered_text)
    if rendered_n == 0:
        js_pct = 0.0
    else:
        gap = max(0, rendered_n - raw_n)
        js_pct = round((gap / rendered_n) * 100, 1)
    return RenderModeDiff(
        raw_text_chars=raw_n,
        rendered_text_chars=rendered_n,
        js_trapped_pct=js_pct,
    )


# ============================================================================
# HTML structure
# ============================================================================


def _html_structure(soup: BeautifulSoup) -> HtmlStructure:
    h1_count = len(soup.find_all("h1"))
    hierarchy_violations = _heading_hierarchy_violations(soup)

    semantic_counts = SemanticTagUsage(
        article=len(soup.find_all("article")),
        section=len(soup.find_all("section")),
        nav=len(soup.find_all("nav")),
        main=len(soup.find_all("main")),
        header=len(soup.find_all("header")),
        footer=len(soup.find_all("footer")),
        aside=len(soup.find_all("aside")),
    )

    text_chars = len(_extract_visible_text(str(soup)))
    html_chars = len(str(soup))
    dom_ratio = round(html_chars / text_chars, 2) if text_chars else 0.0

    images = soup.find_all("img")
    image_count = len(images)
    images_missing_alt = sum(1 for img in images if not (img.get("alt") or "").strip())
    coverage = (
        round(((image_count - images_missing_alt) / image_count) * 100, 1)
        if image_count
        else 100.0
    )

    low_quality = sum(
        1
        for a in soup.find_all("a")
        if (a.get_text() or "").strip().lower() in LOW_QUALITY_LINK_TEXTS
    )

    return HtmlStructure(
        h1_count=h1_count,
        heading_hierarchy_violations=hierarchy_violations,
        semantic_tag_usage=semantic_counts,
        dom_to_content_ratio=dom_ratio,
        image_count=image_count,
        images_missing_alt=images_missing_alt,
        alt_text_coverage_pct=coverage,
        low_quality_link_text_count=low_quality,
    )


def _heading_hierarchy_violations(soup: BeautifulSoup) -> int:
    """A violation is a heading that skips one or more levels below the previous heading."""
    levels = []
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        levels.append(int(tag.name[1]))
    violations = 0
    for prev, curr in zip(levels, levels[1:]):
        if curr - prev > 1:
            violations += 1
    return violations


# ============================================================================
# Structured data (JSON-LD, OG, Twitter)
# ============================================================================


def _structured_data(soup: BeautifulSoup) -> StructuredData:
    json_ld_blocks = 0
    json_ld_types: list[str] = []
    json_ld_valid = True

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        json_ld_blocks += 1
        text = script.string or script.get_text()
        if not text:
            json_ld_valid = False
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            json_ld_valid = False
            continue
        json_ld_types.extend(_extract_jsonld_types(data))

    open_graph: dict[str, bool] = {}
    for meta in soup.find_all("meta"):
        prop = meta.get("property", "")
        if isinstance(prop, str) and prop.startswith("og:"):
            open_graph[prop] = bool((meta.get("content") or "").strip())

    twitter_card = any(
        (meta.get("name") or "").startswith("twitter:") for meta in soup.find_all("meta")
    )

    types_present = set(json_ld_types)
    missing = [s for s in RECOMMENDED_SCHEMAS if s not in types_present]

    return StructuredData(
        json_ld_blocks=json_ld_blocks,
        json_ld_types=sorted(set(json_ld_types)),
        json_ld_valid=json_ld_valid,
        open_graph=open_graph,
        twitter_card=twitter_card,
        missing_recommended_schemas=missing,
    )


def _extract_jsonld_types(data: object) -> list[str]:
    """Walk a parsed JSON-LD payload and collect every @type value."""
    found: list[str] = []
    stack: list[object] = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            t = node.get("@type")
            if isinstance(t, str):
                found.append(t)
            elif isinstance(t, list):
                found.extend(x for x in t if isinstance(x, str))
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


# ============================================================================
# AI crawler access (robots.txt)
# ============================================================================


def _ai_crawler_access(
    robots_text: str | None, robots_present: bool
) -> AiCrawlerAccess:
    crawlers: dict[str, str] = {}
    if not robots_present or robots_text is None:
        for bot in KNOWN_AI_CRAWLERS:
            crawlers[bot] = "allowed"  # absent robots.txt = allow all
        return AiCrawlerAccess(robots_txt_present=False, crawlers=crawlers)  # type: ignore[arg-type]

    rp = RobotFileParser()
    rp.parse(robots_text.splitlines())
    for bot in KNOWN_AI_CRAWLERS:
        crawlers[bot] = "allowed" if rp.can_fetch(bot, "/") else "disallowed"
    return AiCrawlerAccess(robots_txt_present=True, crawlers=crawlers)  # type: ignore[arg-type]


async def _fetch_robots(url: str) -> tuple[str | None, bool]:
    robots_url = urljoin(_root_url(url), "/robots.txt")
    text = await _safe_get_text(robots_url)
    return text, text is not None


# ============================================================================
# llms.txt
# ============================================================================


def _llms_txt(text: str | None, present: bool) -> LlmsTxt:
    if not present or text is None:
        return LlmsTxt(present=False, valid_markdown=None, size_bytes=None)
    size = len(text.encode("utf-8"))
    valid = _looks_like_markdown(text)
    return LlmsTxt(present=True, valid_markdown=valid, size_bytes=size)


def _looks_like_markdown(text: str) -> bool:
    """Crude check: at least one heading or list/link marker."""
    return bool(
        re.search(r"^#{1,6}\s+\S", text, re.MULTILINE)
        or re.search(r"^[-*+]\s+\S", text, re.MULTILINE)
        or re.search(r"\[.+?\]\(.+?\)", text)
    )


async def _fetch_llms(url: str) -> tuple[str | None, bool]:
    llms_url = urljoin(_root_url(url), "/llms.txt")
    text = await _safe_get_text(llms_url)
    return text, text is not None


# ============================================================================
# Trust signals
# ============================================================================


def _trust_signals(url: str, soup: BeautifulSoup) -> TrustSignals:
    https = url.lower().startswith("https://")

    contact = _has_contact_info(soup)
    privacy = _has_privacy_link(soup)
    author = _has_author_byline(soup)
    updated = _has_last_updated(soup)

    return TrustSignals(
        https=https,
        contact_info_present=contact,
        privacy_policy_link=privacy,
        author_byline=author,
        last_updated_date=updated,
    )


def _has_contact_info(soup: BeautifulSoup) -> bool:
    for a in soup.find_all("a"):
        href = (a.get("href") or "").lower()
        text = (a.get_text() or "").strip().lower()
        if href.startswith("mailto:") or href.startswith("tel:"):
            return True
        if "contact" in text or "/contact" in href:
            return True
    return False


def _has_privacy_link(soup: BeautifulSoup) -> bool:
    for a in soup.find_all("a"):
        href = (a.get("href") or "").lower()
        text = (a.get_text() or "").strip().lower()
        if "privacy" in text or "privacy" in href:
            return True
    return False


def _has_author_byline(soup: BeautifulSoup) -> bool:
    if soup.find("meta", attrs={"name": "author"}):
        return True
    if soup.find(attrs={"rel": "author"}):
        return True
    if soup.find(attrs={"itemprop": "author"}):
        return True
    return False


def _has_last_updated(soup: BeautifulSoup) -> bool:
    if soup.find("meta", attrs={"property": "article:modified_time"}):
        return True
    if soup.find(attrs={"itemprop": "dateModified"}):
        return True
    if soup.find("time", attrs={"datetime": True}):
        return True
    return False


# ============================================================================
# Page size
# ============================================================================


def _page_size(raw_html: str, rendered_html: str) -> PageSize:
    """`total_bytes_estimate` is rendered-DOM byte count — a cheap, conservative
    proxy that does not include images, CSS, or JS files. Improving this would
    require recording network responses during the Playwright fetch."""
    return PageSize(
        html_bytes=len(raw_html.encode("utf-8")),
        total_bytes_estimate=len(rendered_html.encode("utf-8")),
    )


# ============================================================================
# Helpers
# ============================================================================


def _extract_visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "nav", "footer"]):
        tag.decompose()
    return " ".join(soup.get_text(separator=" ").split())


def _root_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


async def _safe_get_text(url: str) -> str | None:
    """GET a URL; return text on 200, None on any error or non-200 status."""
    headers = {"User-Agent": _USER_AGENT}
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=_HTTP_TIMEOUT_S, headers=headers
        ) as client:
            resp = await client.get(url)
        if resp.status_code == 200:
            return resp.text
    except (httpx.HTTPError, OSError):
        return None
    return None
