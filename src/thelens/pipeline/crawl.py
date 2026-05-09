"""Multi-URL parallel crawl + per-page audit.

For every URL produced by `discover.py`, fetch raw HTML (httpx) plus
JS-rendered DOM (Playwright) plus a viewport screenshot, then run the
technical/structural audit. Per-page artifacts live in
`runs/<id>/pages/<slug>/`.

A single Playwright browser instance is launched once and reused across
all pages — only Playwright `BrowserContext` instances are created per
page (cheap). Concurrency is capped via a semaphore.

Failures on individual pages don't fail the whole run: a `failed` marker
is written to that page's directory and the rest of the crawl continues.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from playwright.async_api import Browser, async_playwright
from rich.console import Console

from thelens.pipeline import audit as audit_step
from thelens.pipeline.discover import DiscoveredPage, url_to_slug


_log = logging.getLogger(__name__)


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_VIEWPORT = {"width": 1440, "height": 900}
_NETWORKIDLE_EXTRA_WAIT_S = 1.5
_HTTP_TIMEOUT_S = 30.0
_PAGE_TIMEOUT_MS = 45_000

# Polite crawling. WAFs (Cloudflare, Akamai, Imperva) trip on burst traffic
# even from a small number of clients, so we crawl gently:
#   - low concurrency (2 simultaneous browsers)
#   - random per-fetch jitter (1-3s) before each request
# 100 pages takes ~15 min wall time at this pace, but pages come back as
# real content rather than challenge interstitials.
MAX_CONCURRENT = 2
_JITTER_MIN_S = 1.0
_JITTER_MAX_S = 3.0

# Markers used by common WAF / DDoS-protection providers to fingerprint
# their challenge pages. If we see any of these in the rendered DOM we
# treat the fetch as rate-limited rather than a real page.
_WAF_CHALLENGE_PATTERNS = (
    "Just a moment...",
    "Checking your browser before accessing",
    "DDoS protection by Cloudflare",
    "cf-browser-verification",
    "cf-challenge-running",
    "Enable JavaScript and cookies to continue",
    "Pardon Our Interruption",
    "Incapsula incident ID",
    "Sucuri Website Firewall",
    "Access denied | ",
    "Request unsuccessful. Incapsula",
)


def _looks_like_waf_challenge(html: str) -> bool:
    if not html:
        return False
    return any(pat in html for pat in _WAF_CHALLENGE_PATTERNS)


async def crawl_pages(
    pages: list[DiscoveredPage],
    run_dir: Path,
    console: Console | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch + audit every discovered page in parallel.

    Returns a `{slug: page_record}` dict. Each `page_record` has:
        url, depth, is_anchor, slug, status (complete|failed),
        error (only when status=failed)
    Per-page files (raw_html.html, rendered_dom.html, screenshot, audit)
    are written to `run_dir/pages/<slug>/`.

    The full-page screenshot is captured only for the homepage (depth 0)
    to keep run-folder size manageable. Other pages get viewport only.
    """
    pages_dir = run_dir / "pages"
    pages_dir.mkdir(exist_ok=True)
    sem = asyncio.Semaphore(MAX_CONCURRENT)

    completed = 0
    total = len(pages)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            async def _do_one(page: DiscoveredPage) -> tuple[str, dict[str, Any]]:
                slug = url_to_slug(page.url)
                page_dir = pages_dir / slug
                page_dir.mkdir(exist_ok=True)
                async with sem:
                    # Per-fetch jitter so concurrent workers don't request in lockstep.
                    await asyncio.sleep(random.uniform(_JITTER_MIN_S, _JITTER_MAX_S))
                    try:
                        await _fetch_one(browser, page.url, page_dir, full_screenshot=(page.depth == 0))
                    except Exception as exc:
                        _log.warning("crawl: %s failed: %s", page.url, exc)
                        return slug, {
                            "slug": slug,
                            "url": page.url,
                            "depth": page.depth,
                            "is_anchor": page.is_anchor,
                            "status": "failed",
                            "error": str(exc),
                        }

                    rendered_path = page_dir / "rendered_dom.html"
                    rendered_html = (
                        rendered_path.read_text(encoding="utf-8")
                        if rendered_path.exists()
                        else ""
                    )
                    if _looks_like_waf_challenge(rendered_html):
                        _log.warning("crawl: %s blocked by WAF challenge", page.url)
                        return slug, {
                            "slug": slug,
                            "url": page.url,
                            "depth": page.depth,
                            "is_anchor": page.is_anchor,
                            "status": "rate_limited",
                            "error": "WAF / rate-limit challenge page returned (excluded from corpus)",
                        }

                    audit = await audit_step.audit_url(page.url, page_dir)
                    (page_dir / "technical_audit.json").write_text(
                        audit.model_dump_json(indent=2), encoding="utf-8"
                    )
                    return slug, {
                        "slug": slug,
                        "url": page.url,
                        "depth": page.depth,
                        "is_anchor": page.is_anchor,
                        "status": "complete",
                    }

            ok = 0
            rate_limited = 0
            failed = 0

            async def _track(page: DiscoveredPage) -> tuple[str, dict[str, Any]]:
                nonlocal completed, ok, rate_limited, failed
                slug, info = await _do_one(page)
                completed += 1
                status = info.get("status")
                if status == "complete":
                    ok += 1
                elif status == "rate_limited":
                    rate_limited += 1
                else:
                    failed += 1
                if console and (completed % 5 == 0 or completed == total):
                    parts = [f"{ok} ok"]
                    if rate_limited:
                        parts.append(f"{rate_limited} rate-limited")
                    if failed:
                        parts.append(f"{failed} failed")
                    console.print(
                        f"    crawled {completed}/{total} pages "
                        f"[dim]({', '.join(parts)})[/]",
                        highlight=False,
                    )
                return slug, info

            tuples = await asyncio.gather(*[_track(p) for p in pages])
        finally:
            await browser.close()

    results: dict[str, dict[str, Any]] = {}
    for slug, info in tuples:
        # Handle slug collisions: append a suffix if the same slug was used twice.
        if slug in results:
            i = 2
            while f"{slug}_{i}" in results:
                i += 1
            results[f"{slug}_{i}"] = info
        else:
            results[slug] = info

    # Write index alongside the pages folder.
    (run_dir / "discovery.json").write_text(
        json.dumps(
            {
                "discovered_at": datetime.now(timezone.utc).isoformat(),
                "total": len(results),
                "pages": list(results.values()),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return results


async def _fetch_one(
    browser: Browser,
    url: str,
    page_dir: Path,
    full_screenshot: bool,
) -> None:
    """Fetch raw HTML + rendered DOM + screenshot for a single URL."""
    raw_html = await _fetch_raw_html(url)
    (page_dir / "raw_html.html").write_text(raw_html, encoding="utf-8")

    context = await browser.new_context(
        user_agent=_BROWSER_HEADERS["User-Agent"], viewport=_VIEWPORT
    )
    try:
        page = await context.new_page()
        await page.goto(url, wait_until="networkidle", timeout=_PAGE_TIMEOUT_MS)
        await asyncio.sleep(_NETWORKIDLE_EXTRA_WAIT_S)
        html = await page.content()
        (page_dir / "rendered_dom.html").write_text(html, encoding="utf-8")
        await page.screenshot(
            path=str(page_dir / "screenshot_viewport.png"), full_page=False
        )
        if full_screenshot:
            await page.screenshot(
                path=str(page_dir / "screenshot_full.png"), full_page=True
            )
    finally:
        await context.close()


async def _fetch_raw_html(url: str) -> str:
    """GET via httpx with full browser headers. Returns empty string on failure."""
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=_HTTP_TIMEOUT_S, headers=_BROWSER_HEADERS
    ) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text
        except (httpx.HTTPError, OSError):
            return ""
