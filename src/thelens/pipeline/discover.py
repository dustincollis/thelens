"""Discovery: collect structural seeds + a URL pool from a homepage.

Two-phase architecture:
  Phase 1 (this module): figure out what's on the site without crawling
  the whole thing yet. Reads `sitemap.xml` (with `robots.txt` Sitemap:
  directives), extracts `<header>/<nav>/<footer>` anchors from the
  homepage. Returns:
    - `structural_seeds`: the small set of pages we *will* crawl first
      (homepage + nav anchors)
    - `URLPool`: the larger pool of remaining URLs grouped by section,
      ready to be handed to the AI planner

  Phase 2 (pipeline/plan.py): the AI sees what was found in Phase 1 and
  picks additional URLs to crawl from the pool, up to a budget.

Same-domain only (www-stripped). Junk URLs (login, search, cart, files,
pagination, mailto/tel/javascript) are filtered before they reach the
pool. Tracking params (`utm_*`, `fbclid`, etc.) are stripped during
canonicalization.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from thelens.pipeline.sitemap import fetch_sitemap_urls


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


@dataclass(frozen=True)
class DiscoveredPage:
    url: str
    depth: int
    is_anchor: bool
    section: str = "home"


@dataclass
class URLPool:
    """Remaining URLs grouped by section. Section = first non-empty path segment."""

    by_section: dict[str, list[str]] = field(default_factory=dict)

    def total_count(self) -> int:
        return sum(len(v) for v in self.by_section.values())

    def add(self, url: str) -> None:
        section = section_for(url)
        self.by_section.setdefault(section, []).append(url)


def canonicalize(url: str) -> str:
    """Normalize URL for deduplication."""
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
    """Filesystem-safe directory name for an URL.

    Homepage maps to `_home` (the underscore prefix sorts it to the top
    of `pages/` listings and avoids any chance of colliding with a real
    `/home` path on a site).
    """
    p = urlparse(url)
    path = p.path.strip("/")
    if not path:
        return "_home"
    slug = path.lower().replace("/", "_")
    slug = re.sub(r"[^a-z0-9_-]+", "_", slug)
    slug = slug.strip("_") or "_home"
    return slug[:80]


def section_for(url: str) -> str:
    """Categorize a URL by its first non-empty path segment.

    `https://example.com/services/ai` → `services`
    `https://example.com/`             → `_home`
    `https://example.com/blog/post-1`  → `blog`
    """
    p = urlparse(url)
    segments = [s for s in p.path.split("/") if s]
    if not segments:
        return "home"
    return segments[0].lower()


def _extract_links(html: str, base_url: str, restrict_to_chrome: bool) -> list[str]:
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


async def discover(
    homepage_url: str,
    max_pages: int = 100,
) -> tuple[list[DiscoveredPage], URLPool]:
    """Phase 1 discovery.

    Returns `(structural_seeds, url_pool)`:
      - structural_seeds = homepage + every link found in homepage
        `<header>/<nav>/<footer>`. These are the pages we crawl first.
      - url_pool = every other same-domain URL found via the site's
        sitemap, grouped by section. The AI planner picks additional
        crawl targets from this pool in Phase 2.

    Anchor seeds are deduplicated against the URL pool, so a URL listed
    in both nav and sitemap appears only in `structural_seeds`.
    """
    home_canonical = canonicalize(homepage_url)
    structural: dict[str, DiscoveredPage] = {
        home_canonical: DiscoveredPage(
            home_canonical, depth=0, is_anchor=True, section="home"
        )
    }

    async with httpx.AsyncClient(
        follow_redirects=True, headers=_DEFAULT_HEADERS
    ) as client:
        homepage_html = await _fetch_html(client, homepage_url)

    if homepage_html:
        for link in _extract_links(homepage_html, homepage_url, restrict_to_chrome=True):
            c = canonicalize(link)
            if c in structural:
                continue
            if not same_domain(c, homepage_url) or not is_crawlable(c):
                continue
            structural[c] = DiscoveredPage(
                c, depth=1, is_anchor=True, section=section_for(c)
            )
    else:
        _log.warning(
            "discover: homepage fetch returned nothing; structural seeds = home only"
        )

    pool = URLPool()
    sitemap_urls = await fetch_sitemap_urls(homepage_url)
    _log.info("discover: sitemap returned %d URLs", len(sitemap_urls))

    for url in sitemap_urls:
        c = canonicalize(url)
        if c in structural:
            continue
        if not same_domain(c, homepage_url) or not is_crawlable(c):
            continue
        pool.add(c)

    # Cap structural seeds at half of max_pages so the AI planner has
    # meaningful budget left even on sites with very rich navigation.
    # min() ensures the cap never exceeds the overall budget on small runs.
    structural_cap = min(max_pages, max(20, max_pages // 2))
    if len(structural) > structural_cap:
        # Keep homepage + the first (structural_cap - 1) anchor pages
        # in the order they appeared in the homepage DOM.
        kept: dict[str, DiscoveredPage] = {}
        for url, page in structural.items():
            kept[url] = page
            if len(kept) >= structural_cap:
                break
        # The remaining anchor pages get pushed into the pool so the AI
        # can still pick them if it wants.
        for url, page in structural.items():
            if url not in kept:
                pool.add(url)
        structural = kept

    return list(structural.values()), pool


def enrich_pool_from_crawled_pages(
    run_dir,
    pool: URLPool,
    structural_pages: list[DiscoveredPage],
    homepage_url: str,
) -> URLPool:
    """After Phase 1 crawl, mine the rendered DOMs for additional URLs.

    For sites whose sitemap is gated by a WAF (or that don't publish one),
    this is how the planner discovers what's actually on the site —
    walking the links present in the rendered HTML of pages we already
    fetched. URLs already in `structural_pages` or `pool` are skipped.
    """
    structural_urls = {p.url for p in structural_pages}
    in_pool: set[str] = set()
    for urls in pool.by_section.values():
        in_pool.update(urls)

    pages_dir = run_dir / "pages"
    if not pages_dir.exists():
        return pool

    for page in structural_pages:
        rendered = pages_dir / url_to_slug(page.url) / "rendered_dom.html"
        if not rendered.exists():
            continue
        html = rendered.read_text(encoding="utf-8")
        for link in _extract_links(html, page.url, restrict_to_chrome=False):
            c = canonicalize(link)
            if c in structural_urls or c in in_pool:
                continue
            if not same_domain(c, homepage_url) or not is_crawlable(c):
                continue
            pool.add(c)
            in_pool.add(c)
    return pool
