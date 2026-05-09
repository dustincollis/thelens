"""Multi-URL parallel crawl + per-page audit, with adaptive backoff.

For every URL passed in, fetches raw HTML (httpx) plus JS-rendered DOM
(Playwright) plus a viewport screenshot, then runs the technical/
structural audit. Per-page artifacts live in `runs/<id>/pages/<slug>/`.

Polite by default — concurrency=2, jitter 1-3s — and adaptive: if a
sliding window of recent fetches sees too many WAF challenge pages, the
crawler slows further (concurrency=1, jitter 3-8s, periodic 60s pauses)
for the rest of the run. The crawl is meant to be the durable artifact
the AI work iterates against, so it's biased toward "go slow, get clean
content" rather than "go fast."

Failures on individual pages are non-fatal: the page gets a `failed`
or `rate_limited` marker JSON in its directory and the rest of the
crawl continues. Crawl progress prints `ok / rate-limited / failed`
counts as it goes so trouble is visible in real time.

The discovery list can come from either Phase 1 (structural seeds) or
Phase 2 (AI-planned URLs); this module just takes a list of pages and
crawls them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections import deque
from dataclasses import dataclass, field
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

# Default polite-mode pacing.
_DEFAULT_CONCURRENT = 2
_DEFAULT_JITTER_MIN_S = 1.0
_DEFAULT_JITTER_MAX_S = 3.0

# Cautious-mode pacing (triggered after we hit too many WAF challenges).
_CAUTIOUS_JITTER_MIN_S = 3.0
_CAUTIOUS_JITTER_MAX_S = 8.0
_CAUTIOUS_COOLDOWN_S = 60.0

# Sliding-window WAF detection: if N of last M fetches were rate-limited,
# switch to cautious mode for the rest of the run.
_WAF_WINDOW = 10
_WAF_THRESHOLD = 3

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


@dataclass
class _CrawlState:
    """Shared adaptive-backoff state across crawl workers."""

    recent_outcomes: deque = field(
        default_factory=lambda: deque(maxlen=_WAF_WINDOW)
    )  # True = rate-limited, False = ok
    cautious: bool = False
    cooldown_until: float = 0.0  # asyncio loop time

    def record(self, was_rate_limited: bool) -> None:
        self.recent_outcomes.append(was_rate_limited)
        if (
            not self.cautious
            and sum(self.recent_outcomes) >= _WAF_THRESHOLD
        ):
            self.cautious = True
            self.cooldown_until = asyncio.get_event_loop().time() + _CAUTIOUS_COOLDOWN_S

    def jitter_range(self) -> tuple[float, float]:
        if self.cautious:
            return _CAUTIOUS_JITTER_MIN_S, _CAUTIOUS_JITTER_MAX_S
        return _DEFAULT_JITTER_MIN_S, _DEFAULT_JITTER_MAX_S

    async def wait_for_cooldown(self) -> None:
        now = asyncio.get_event_loop().time()
        if now < self.cooldown_until:
            await asyncio.sleep(self.cooldown_until - now)


async def crawl_pages(
    pages: list[DiscoveredPage],
    run_dir: Path,
    console: Console | None = None,
    state: _CrawlState | None = None,
) -> tuple[dict[str, dict[str, Any]], _CrawlState]:
    """Fetch + audit every page in parallel. Returns `(results, state)`.

    `state` carries forward across multi-phase crawls (Phase 1 + Phase 2)
    so adaptive backoff persists across both. Pass it back in on the
    second call.

    Per-page files (raw_html.html, rendered_dom.html, screenshot, audit)
    are written to `run_dir/pages/<slug>/`. The full-page screenshot is
    captured only for the homepage (depth 0). discovery.json at the
    run root is always rewritten with the union of crawled pages.
    """
    pages_dir = run_dir / "pages"
    pages_dir.mkdir(exist_ok=True)
    state = state or _CrawlState()
    sem = asyncio.Semaphore(_DEFAULT_CONCURRENT)

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
                    await state.wait_for_cooldown()
                    jmin, jmax = state.jitter_range()
                    await asyncio.sleep(random.uniform(jmin, jmax))
                    try:
                        await _fetch_one(
                            browser, page.url, page_dir,
                            full_screenshot=(page.depth == 0),
                        )
                    except Exception as exc:
                        state.record(was_rate_limited=False)
                        _log.warning("crawl: %s failed: %s", page.url, exc)
                        return slug, _record(page, slug, "failed", error=str(exc))

                    rendered_path = page_dir / "rendered_dom.html"
                    rendered_html = (
                        rendered_path.read_text(encoding="utf-8")
                        if rendered_path.exists()
                        else ""
                    )
                    if _looks_like_waf_challenge(rendered_html):
                        state.record(was_rate_limited=True)
                        _log.warning("crawl: %s blocked by WAF challenge", page.url)
                        return slug, _record(
                            page, slug, "rate_limited",
                            error="WAF / rate-limit challenge page returned",
                        )

                    state.record(was_rate_limited=False)
                    audit = await audit_step.audit_url(page.url, page_dir)
                    (page_dir / "technical_audit.json").write_text(
                        audit.model_dump_json(indent=2), encoding="utf-8"
                    )
                    return slug, _record(page, slug, "complete")

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
                    suffix = " [yellow](cautious mode)[/]" if state.cautious else ""
                    console.print(
                        f"    crawled {completed}/{total} pages "
                        f"[dim]({', '.join(parts)})[/]{suffix}",
                        highlight=False,
                    )
                return slug, info

            tuples = await asyncio.gather(*[_track(p) for p in pages])
        finally:
            await browser.close()

    # Merge with any existing discovery.json (Phase 2 appends to Phase 1's).
    results: dict[str, dict[str, Any]] = {}
    discovery_path = run_dir / "discovery.json"
    if discovery_path.exists():
        try:
            existing = json.loads(discovery_path.read_text(encoding="utf-8"))
            for entry in existing.get("pages", []):
                results[entry["slug"]] = entry
        except (json.JSONDecodeError, OSError):
            pass

    for slug, info in tuples:
        if slug in results and info.get("status") == "complete":
            # Phase 2 may re-crawl a Phase 1 slug only if structural cap
            # pushed it; prefer the newer record.
            results[slug] = info
        elif slug not in results:
            results[slug] = info

    discovery_path.write_text(
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
    return results, state


def _record(
    page: DiscoveredPage,
    slug: str,
    status: str,
    error: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "slug": slug,
        "url": page.url,
        "depth": page.depth,
        "is_anchor": page.is_anchor,
        "section": page.section,
        "status": status,
    }
    if error:
        out["error"] = error
    return out


async def _fetch_one(
    browser: Browser,
    url: str,
    page_dir: Path,
    full_screenshot: bool,
) -> None:
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
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=_HTTP_TIMEOUT_S, headers=_BROWSER_HEADERS
    ) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text
        except (httpx.HTTPError, OSError):
            return ""
