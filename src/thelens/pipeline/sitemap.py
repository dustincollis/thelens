"""Sitemap discovery and parsing.

Walks `robots.txt` for `Sitemap:` directives, then `/sitemap.xml`
fallbacks, recursively follows sitemap-index files, and returns a flat
list of canonical URLs found.

Cheap (httpx + XML), tolerant of failures: missing or broken sitemaps
just produce an empty list, not an error.
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup


_log = logging.getLogger(__name__)


_FALLBACK_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml")
_TIMEOUT_S = 20.0
_MAX_SITEMAP_DEPTH = 5  # safety guard against pathological sitemap-index loops

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "application/xml, text/xml, */*;q=0.9",
}


async def fetch_sitemap_urls(homepage_url: str) -> list[str]:
    """Discover every URL listed in the site's sitemaps.

    Strategy: try `robots.txt` first (its `Sitemap:` directives are the
    site's authoritative pointer); fall back to common `/sitemap.xml`
    paths if robots doesn't list any. Recursively expands sitemap-index
    files. Returns a sorted list of unique URLs.
    """
    parsed = urlparse(homepage_url)
    root = f"{parsed.scheme}://{parsed.netloc}"

    sitemap_seeds: list[str] = []
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=_TIMEOUT_S, headers=_HEADERS
    ) as client:
        sitemap_seeds.extend(await _sitemaps_from_robots(client, root))

        if not sitemap_seeds:
            for path in _FALLBACK_PATHS:
                candidate = urljoin(root, path)
                if await _looks_like_sitemap(client, candidate):
                    sitemap_seeds.append(candidate)
                    break

        if not sitemap_seeds:
            return []

        all_urls: set[str] = set()
        seen_sitemaps: set[str] = set()
        # (url, depth) — depth caps recursion through sitemap-index files
        queue: list[tuple[str, int]] = [(u, 0) for u in sitemap_seeds]
        while queue:
            sm_url, depth = queue.pop(0)
            if sm_url in seen_sitemaps or depth > _MAX_SITEMAP_DEPTH:
                continue
            seen_sitemaps.add(sm_url)

            xml_text = await _fetch_text(client, sm_url)
            if not xml_text:
                continue

            child_sitemaps, page_urls = _parse_sitemap_xml(xml_text)
            for child in child_sitemaps:
                queue.append((child, depth + 1))
            all_urls.update(page_urls)

    return sorted(all_urls)


async def _sitemaps_from_robots(client: httpx.AsyncClient, root: str) -> list[str]:
    text = await _fetch_text(client, urljoin(root, "/robots.txt"))
    if not text:
        return []
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("sitemap:"):
            url = line.split(":", 1)[1].strip()
            if url:
                out.append(url)
    return out


async def _looks_like_sitemap(client: httpx.AsyncClient, url: str) -> bool:
    text = await _fetch_text(client, url)
    if not text:
        return False
    sniff = text.lstrip()[:200].lower()
    return ("<urlset" in sniff) or ("<sitemapindex" in sniff)


async def _fetch_text(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            return None
        return resp.text
    except (httpx.HTTPError, OSError) as exc:
        _log.warning("sitemap: fetch %s failed: %s", url, exc)
        return None


def _parse_sitemap_xml(xml: str) -> tuple[list[str], list[str]]:
    """Parse a sitemap or sitemap-index XML.

    Returns `(child_sitemap_urls, page_urls)`. lxml-xml is namespace-aware
    so the standard `xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"`
    is handled without explicit namespace bookkeeping.
    """
    try:
        soup = BeautifulSoup(xml, "lxml-xml")
    except Exception:
        return [], []

    child_sitemaps: list[str] = []
    for sm in soup.find_all("sitemap"):
        loc = sm.find("loc")
        if loc and loc.text:
            child_sitemaps.append(loc.text.strip())

    page_urls: list[str] = []
    for url_tag in soup.find_all("url"):
        loc = url_tag.find("loc")
        if loc and loc.text:
            page_urls.append(loc.text.strip())

    return child_sitemaps, page_urls
