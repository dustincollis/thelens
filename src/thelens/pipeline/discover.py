"""Discovery: from one homepage URL, produce up to N target URLs to crawl.

Strategy ("tree + 2 deep"):
  depth 0: the homepage itself
  depth 1: links inside <header>, <nav>, <footer> of the homepage (anchors)
  depth 2: links found on each depth-1 page

Same-domain only (www-stripped). Cap at `max_pages`. Junk URLs (login,
search, cart, files, pagination, mailto/tel/javascript) are filtered.

Uses httpx for discovery — fast, and nav is server-rendered on virtually
every marketing site. Full Playwright fetch (with screenshots and JS DOM)
happens later in crawl.py against the URLs this module returns.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup


_log = logging.getLogger(__name__)


_FILE_EXT_RE = re.compile(
    r"\.(pdf|jpg|jpeg|png|gif|svg|webp|zip|tar|gz|mp3|mp4|wav|mov|webm|"
    r"css|js|json|xml|ico|woff|woff2|ttf|eot|rss|atom)(\?|$)",
    re.I,
)
_JUNK_PATH_RE = re.compile(
    r"/(login|signin|sign-in|signup|sign-up|register|logout|sign-out|"
    r"cart|checkout|account|profile|search|404|500)(/|\?|$)",
    re.I,
)
_PAGINATION_RE = re.compile(r"[?&]page=\d+|/page/\d+", re.I)

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "mc_cid", "mc_eid",
}

_DEFAULT_HEADERS = {
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

_DISCOVERY_TIMEOUT_S = 15.0
_DEPTH_2_CONCURRENCY = 8


@dataclass(frozen=True)
class DiscoveredPage:
    url: str
    depth: int
    is_anchor: bool


def canonicalize(url: str) -> str:
    """Normalize URL for deduplication.

    Strips fragments, lowercases scheme/host, drops `www.`, drops common
    tracking params, normalizes trailing slash, sorts surviving query keys.
    """
    p = urlparse(url)
    scheme = (p.scheme or "https").lower()
    netloc = p.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = p.path.rstrip("/") or "/"
    if p.query:
        kept = []
        for kv in p.query.split("&"):
            if not kv:
                continue
            k = kv.split("=", 1)[0].lower()
            if k in _TRACKING_PARAMS:
                continue
            kept.append(kv)
        query = "&".join(sorted(kept))
    else:
        query = ""
    return urlunparse((scheme, netloc, path, "", query, ""))


def same_domain(url: str, root: str) -> bool:
    def host(u: str) -> str:
        h = urlparse(u).netloc.lower()
        return h[4:] if h.startswith("www.") else h
    return host(url) == host(root)


def is_crawlable(url: str) -> bool:
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return False
    if _FILE_EXT_RE.search(url):
        return False
    if _JUNK_PATH_RE.search(url):
        return False
    if _PAGINATION_RE.search(url):
        return False
    return True


def url_to_slug(url: str) -> str:
    """Filesystem-safe directory name from a canonical URL.

    `https://epam.com/` → `_home`
    `https://epam.com/services/ai` → `services_ai`
    """
    p = urlparse(url)
    path = p.path.strip("/")
    if not path:
        return "_home"
    slug = path.lower().replace("/", "_")
    slug = re.sub(r"[^a-z0-9_-]+", "_", slug)
    slug = slug.strip("_") or "_home"
    return slug[:80]


def _extract_links(html: str, base_url: str, restrict_to_chrome: bool) -> list[str]:
    """Pull `<a href>` URLs from HTML, resolved against `base_url`.

    `restrict_to_chrome=True` returns only links inside `<header>`, `<nav>`,
    or `<footer>` (used for the depth-1 anchor pass).
    """
    soup = BeautifulSoup(html, "lxml")
    if restrict_to_chrome:
        roots = soup.find_all(["header", "nav", "footer"])
        if not roots:
            roots = [soup]
    else:
        roots = [soup]

    urls: list[str] = []
    for root in roots:
        for a in root.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            urls.append(urljoin(base_url, href))
    return urls


async def _fetch_html(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        resp = await client.get(url, timeout=_DISCOVERY_TIMEOUT_S)
        if resp.status_code != 200:
            return None
        return resp.text
    except Exception as exc:
        _log.warning("discover: fetch failed for %s: %s", url, exc)
        return None


async def discover(homepage_url: str, max_pages: int = 100) -> list[DiscoveredPage]:
    """Discover up to `max_pages` URLs reachable from the homepage.

    Always includes the homepage as depth 0 even if its fetch fails. Anchor
    URLs (links found in homepage `<nav>/<header>/<footer>`) come back as
    depth 1; links found from those are depth 2.
    """
    home_canonical = canonicalize(homepage_url)
    pages: dict[str, DiscoveredPage] = {
        home_canonical: DiscoveredPage(home_canonical, depth=0, is_anchor=True)
    }

    async with httpx.AsyncClient(
        follow_redirects=True, headers=_DEFAULT_HEADERS
    ) as client:
        homepage_html = await _fetch_html(client, homepage_url)
        if not homepage_html:
            _log.warning("discover: homepage fetch returned nothing; returning home only")
            return list(pages.values())

        for link in _extract_links(homepage_html, homepage_url, restrict_to_chrome=True):
            c = canonicalize(link)
            if c in pages or not same_domain(c, homepage_url) or not is_crawlable(c):
                continue
            pages[c] = DiscoveredPage(c, depth=1, is_anchor=True)
            if len(pages) >= max_pages:
                return list(pages.values())

        anchor_pages = [p for p in pages.values() if p.depth == 1]
        sem = asyncio.Semaphore(_DEPTH_2_CONCURRENCY)

        async def _fetch_and_extract(p: DiscoveredPage) -> list[str]:
            async with sem:
                html = await _fetch_html(client, p.url)
            return _extract_links(html, p.url, restrict_to_chrome=False) if html else []

        results = await asyncio.gather(
            *[_fetch_and_extract(p) for p in anchor_pages]
        )
        for links in results:
            for link in links:
                c = canonicalize(link)
                if c in pages or not same_domain(c, homepage_url) or not is_crawlable(c):
                    continue
                pages[c] = DiscoveredPage(c, depth=2, is_anchor=False)
                if len(pages) >= max_pages:
                    return list(pages.values())

    return list(pages.values())
