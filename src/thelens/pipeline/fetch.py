"""Step 1: fetch raw HTML and JS-rendered DOM, capture screenshots."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from playwright.async_api import async_playwright


_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

_VIEWPORT = {"width": 1440, "height": 900}
_NETWORKIDLE_EXTRA_WAIT_S = 2.0
_HTTP_TIMEOUT_S = 30.0
_PAGE_TIMEOUT_MS = 60_000


async def fetch_all(url: str, run_dir: Path) -> None:
    """Both fetches plus both screenshots, written into `run_dir`."""
    raw_html = await fetch_raw_html(url)
    (run_dir / "raw_html.html").write_text(raw_html, encoding="utf-8")
    await fetch_rendered(url, run_dir)


async def fetch_raw_html(url: str) -> str:
    headers = {"User-Agent": _USER_AGENT, "Accept": "text/html,*/*"}
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=_HTTP_TIMEOUT_S, headers=headers
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


async def fetch_rendered(url: str, run_dir: Path) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                user_agent=_USER_AGENT, viewport=_VIEWPORT
            )
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=_PAGE_TIMEOUT_MS)
            await asyncio.sleep(_NETWORKIDLE_EXTRA_WAIT_S)

            html = await page.content()
            (run_dir / "rendered_dom.html").write_text(html, encoding="utf-8")

            await page.screenshot(
                path=str(run_dir / "screenshot_viewport.png"), full_page=False
            )
            await page.screenshot(
                path=str(run_dir / "screenshot_full.png"), full_page=True
            )
        finally:
            await browser.close()
