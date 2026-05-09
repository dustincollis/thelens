"""Phase 2: AI-driven crawl planning.

Given the pages already crawled in Phase 1 plus a URL pool of remaining
candidates (from sitemap + nav extraction), one synthesis-grade LLM call
picks which additional URLs to crawl. The output is a `CrawlPlan` that
includes the selected URLs, their distribution across sections, and the
model's rationale.

This is the "intelligence" injection in the discovery process — instead
of a fixed BFS depth or a sitemap-everything dump, the AI evaluates what
it has seen and what's outstanding before more crawling happens.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from rich.console import Console

from thelens.config import SynthesisConfig, prompts_dir
from thelens.llm.base import load_prompt
from thelens.llm.factory import build_client
from thelens.llm.retry import with_retry
from thelens.models import CrawlPlan, UsageInfo
from thelens.pipeline._extract import extract_title, extract_visible_text
from thelens.pipeline.discover import (
    DiscoveredPage,
    URLPool,
    canonicalize,
    is_crawlable,
    same_domain,
    section_for,
    url_to_slug,
)


_log = logging.getLogger(__name__)

# Cap on URLs shown to the AI per section. Most sections have far fewer
# than this; sections with hundreds (blog/insights archives) get sampled
# down so the prompt stays a reasonable size.
_MAX_SAMPLE_PER_SECTION = 50

# Cap on per-page text included in the "what we've crawled" summary.
_PER_PAGE_SUMMARY_CHARS = 400


async def plan_additional_crawl(
    run_dir: Path,
    homepage_url: str,
    structural_pages: list[DiscoveredPage],
    pool: URLPool,
    budget_remaining: int,
    synthesis: SynthesisConfig,
    console: Console | None = None,
) -> tuple[list[DiscoveredPage], UsageInfo | None]:
    """Run the planning call. Returns the selected `DiscoveredPage`s + usage.

    If `budget_remaining` is 0 or the pool is empty, returns `([], None)`
    without an LLM call.
    """
    if budget_remaining <= 0 or pool.total_count() == 0:
        return [], None

    crawled_summary = _build_crawled_summary(run_dir, structural_pages)
    pool_summary = _build_pool_summary(pool)

    prompt = load_prompt(prompts_dir() / "00_crawl_plan.md")
    system, user = prompt.render(
        site_url=homepage_url,
        budget_remaining=budget_remaining,
        crawled_summary_json=json.dumps(crawled_summary, indent=2),
        pool_summary_json=json.dumps(pool_summary, indent=2),
    )

    client = build_client(synthesis.provider, synthesis.model)
    plan, usage = await with_retry(
        lambda: client.complete(
            system=system,
            user=user,
            response_format=CrawlPlan,
            max_tokens=prompt.default_max_tokens,
            temperature=prompt.default_temperature,
        ),
        op_name="crawl_plan",
    )
    assert isinstance(plan, CrawlPlan)

    # Persist the plan for transparency / debugging.
    (run_dir / "crawl_plan.json").write_text(
        plan.model_dump_json(indent=2), encoding="utf-8"
    )

    valid_pool: set[str] = set()
    for urls in pool.by_section.values():
        valid_pool.update(urls)

    selected: list[DiscoveredPage] = []
    seen_slugs: set[str] = set()
    for url in plan.additional_urls:
        c = canonicalize(url)
        if c not in valid_pool:
            _log.warning("plan: AI returned URL not in pool, skipping: %s", c)
            continue
        if not is_crawlable(c) or not same_domain(c, homepage_url):
            continue
        slug = url_to_slug(c)
        # Avoid slug collisions with already-crawled structural pages
        if any(p.url == c for p in structural_pages):
            continue
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        selected.append(
            DiscoveredPage(c, depth=2, is_anchor=False, section=section_for(c))
        )
        if len(selected) >= budget_remaining:
            break

    if console:
        console.print(
            f"    plan: AI selected {len(selected)} URLs across "
            f"{len(plan.by_section)} sections "
            f"[dim](skipped: {', '.join(plan.skipped_sections) or 'none'})[/]"
        )

    return selected, usage


def _build_crawled_summary(
    run_dir: Path, pages: list[DiscoveredPage]
) -> list[dict]:
    out: list[dict] = []
    for page in pages:
        rendered = run_dir / "pages" / url_to_slug(page.url) / "rendered_dom.html"
        if not rendered.exists():
            out.append({
                "url": page.url,
                "section": page.section,
                "title": "",
                "summary": "(not crawled or failed)",
            })
            continue
        html = rendered.read_text(encoding="utf-8")
        out.append({
            "url": page.url,
            "section": page.section,
            "title": extract_title(html)[:120],
            "summary": extract_visible_text(html)[:_PER_PAGE_SUMMARY_CHARS],
        })
    return out


def _build_pool_summary(pool: URLPool) -> dict[str, dict]:
    """Summarize the URL pool for the AI: per-section count + URL sample.

    URLs within a section are sorted by path depth ascending so the
    sample biases toward landing/parent pages over deep leaves — those
    are typically the higher-signal pages for an audit.
    """
    summary: dict[str, dict] = {}
    for section, urls in pool.by_section.items():
        sorted_urls = sorted(urls, key=_path_depth)
        summary[section] = {
            "total_count": len(urls),
            "shown_count": min(_MAX_SAMPLE_PER_SECTION, len(urls)),
            "urls": sorted_urls[:_MAX_SAMPLE_PER_SECTION],
        }
    return summary


def _path_depth(url: str) -> int:
    from urllib.parse import urlparse
    return len([s for s in urlparse(url).path.split("/") if s])
