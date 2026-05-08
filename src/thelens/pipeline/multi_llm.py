"""Steps 5–6 — Layer 3: multi-LLM page-aware and page-blind evaluation.

Page-aware (5): for each enabled provider, the standard question set is
sent with the page text. Provider returns a structured answer object.

Page-blind (6): one synthesis call generates 4–6 category-level queries
from the classification; each query then runs against each enabled provider
WITHOUT showing the page. Responses are post-processed to detect whether
the brand surfaces.

Verification: a separate pass per provider's page-aware response, checking
each claim against the page text. Result is appended to the page-aware
JSON file as `hallucination_flags`.

Concurrency: one `asyncio.Semaphore` per provider (default 4). Different
providers run in parallel; queries within one provider are serialized so a
provider-specific rate limit can't blow up. Every LLM call goes through
`with_retry()` for 429/5xx/timeout backoff.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from rich.console import Console

from thelens.config import (
    ProviderConfig,
    SynthesisConfig,
    build_page_aware_answers_model,
    prompts_dir,
)
from thelens.llm.anthropic_client import AnthropicClient
from thelens.llm.base import LLMClient, load_prompt
from thelens.llm.retry import with_retry
from thelens.models import (
    Classification,
    PageAwareResponse,
    PageBlindQueryResult,
    PageBlindQuerySet,
    PageBlindResponse,
    Question,
    UsageInfo,
    VerificationResult,
)
from thelens.pipeline._extract import extract_title, extract_visible_text


_log = logging.getLogger(__name__)
_PAGE_TEXT_CAP = 100_000

_PAGE_AWARE_SYSTEM = (
    "You are an expert website reviewer. You evaluate a single page against "
    "a structured question set and return JSON only — no preamble, no "
    "markdown fences. Be specific and honest. If a question cannot be "
    "answered from the page content, say so directly in the answer rather "
    "than inventing details."
)

_PAGE_BLIND_SYSTEM = (
    "You are answering a user's question naturally. Reply as you normally "
    "would to a real user — recommend specific companies or solutions when "
    "appropriate, and explain your reasoning briefly. Do not refuse to "
    "name specific brands unless the question is genuinely ambiguous."
)


def _build_client(name: str, model: str) -> LLMClient:
    """Provider-name → LLMClient. Add new providers here."""
    if name == "anthropic":
        return AnthropicClient(model=model)
    raise ValueError(f"unknown / not-yet-implemented provider: {name}")


def _brand_id(url: str) -> str:
    """Domain root for substring brand-mention checking."""
    netloc = urlparse(url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc.split(".")[0]


def _build_page_aware_user_prompt(
    url: str, page_title: str, page_text: str, questions: list[Question]
) -> str:
    parts = [
        f"URL: {url}",
        f"Page title: {page_title}",
        "",
        "Page text:",
        "---",
        page_text,
        "---",
        "",
        "Answer the following questions about this page. Each answer must "
        "match the schema for its type.",
        "",
    ]
    for q in questions:
        line = f"- {q.id} ({q.type}): {q.prompt}"
        if q.count:
            line += f" Return exactly {q.count} items."
        parts.append(line)
    parts.extend(
        [
            "",
            "Schema for each type:",
            "- text: a string",
            "- list: an array of strings",
            '- score: { "score": <integer 1-10>, "justification": <string> }',
            '- boolean_with_explanation: { "value": <true|false>, '
            '"explanation": <string> }',
            "",
            "Return JSON with one field per question id, matching the "
            "corresponding type schema.",
        ]
    )
    return "\n".join(parts)


# ============================================================================
# Page-aware (Step 5)
# ============================================================================


async def run_page_aware(
    run_dir: Path,
    url: str,
    providers: list[ProviderConfig],
    questions: list[Question],
    console: Console,
) -> list[UsageInfo]:
    rendered_html = (run_dir / "rendered_dom.html").read_text(encoding="utf-8")
    page_text = extract_visible_text(rendered_html)[:_PAGE_TEXT_CAP]
    page_title = extract_title(rendered_html)

    answers_model = build_page_aware_answers_model(questions)
    user_prompt = _build_page_aware_user_prompt(url, page_title, page_text, questions)

    sems = {p.name: asyncio.Semaphore(p.max_concurrent) for p in providers}

    async def _one(p: ProviderConfig) -> UsageInfo | None:
        target = run_dir / "llm" / f"{p.name}_page_aware.json"
        async with sems[p.name]:
            client = _build_client(p.name, p.model)
            try:
                started = datetime.now(timezone.utc)
                parsed, usage = await with_retry(
                    lambda: client.complete(
                        system=_PAGE_AWARE_SYSTEM,
                        user=user_prompt,
                        response_format=answers_model,
                        max_tokens=4000,
                    ),
                    op_name=f"page_aware/{p.name}",
                )
                received = datetime.now(timezone.utc)
                response = PageAwareResponse(
                    provider=p.name,
                    model=p.model,
                    requested_at=started,
                    response_received_at=received,
                    answers=parsed.model_dump(),
                    usage=usage,
                )
                target.write_text(response.model_dump_json(indent=2), encoding="utf-8")
                console.print(f"    page_aware/{p.name} [green]ok[/]")
                return usage
            except Exception as exc:
                _write_failed(target, str(exc))
                console.print(f"    page_aware/{p.name} [red]failed[/]: {exc}")
                return None

    results = await asyncio.gather(*[_one(p) for p in providers])
    return [u for u in results if u is not None]


# ============================================================================
# Page-blind (Step 6)
# ============================================================================


async def run_page_blind_query_generation(
    run_dir: Path,
    classification: Classification,
    synthesis: SynthesisConfig,
) -> tuple[PageBlindQuerySet, UsageInfo]:
    prompt = load_prompt(prompts_dir() / "03_page_blind_query_generation.md")
    system, user = prompt.render(
        classification_json=classification.model_dump_json(indent=2)
    )
    client = _build_client(synthesis.provider, synthesis.model)
    parsed, usage = await with_retry(
        lambda: client.complete(
            system=system,
            user=user,
            response_format=PageBlindQuerySet,
            max_tokens=prompt.default_max_tokens,
            temperature=prompt.default_temperature,
        ),
        op_name="page_blind_query_gen",
    )
    assert isinstance(parsed, PageBlindQuerySet)
    (run_dir / "page_blind_queries.json").write_text(
        parsed.model_dump_json(indent=2), encoding="utf-8"
    )
    return parsed, usage


_NUMBERED_LIST_RE = re.compile(r"^\s*(\d+)[\.\)]\s+(.*)$", re.MULTILINE)


def _detect_brand_mention(
    text: str, brand: str, expected_competitors: list[str]
) -> tuple[bool, int | None, list[str]]:
    lower = text.lower()
    brand_lower = brand.lower()
    brand_mentioned = brand_lower in lower

    position: int | None = None
    if brand_mentioned:
        for match in _NUMBERED_LIST_RE.finditer(text):
            if brand_lower in match.group(2).lower():
                position = int(match.group(1))
                break

    competitors_mentioned = sorted(
        {c for c in expected_competitors if c.lower() in lower}
    )
    return brand_mentioned, position, competitors_mentioned


async def run_page_blind(
    run_dir: Path,
    url: str,
    queries: PageBlindQuerySet,
    providers: list[ProviderConfig],
    console: Console,
) -> list[UsageInfo]:
    brand = _brand_id(url)
    sems = {p.name: asyncio.Semaphore(p.max_concurrent) for p in providers}

    async def _one_provider(p: ProviderConfig) -> UsageInfo | None:
        target = run_dir / "llm" / f"{p.name}_page_blind.json"
        client = _build_client(p.name, p.model)
        results: list[PageBlindQueryResult] = []
        total_input = 0
        total_output = 0
        total_cost = 0.0
        started = datetime.now(timezone.utc)
        try:
            for q in queries.queries:
                async with sems[p.name]:
                    text, qusage = await with_retry(
                        lambda q=q: client.complete_text(
                            system=_PAGE_BLIND_SYSTEM,
                            user=q.query_text,
                            max_tokens=2000,
                            disable_web_search=True,
                        ),
                        op_name=f"page_blind/{p.name}/{q.id}",
                    )
                mentioned, position, competitors = _detect_brand_mention(
                    text, brand, q.expected_competitors
                )
                results.append(
                    PageBlindQueryResult(
                        query_id=q.id,
                        query_text=q.query_text,
                        response_text=text,
                        brand_mentioned=mentioned,
                        mention_position=position,
                        competitors_mentioned=competitors,
                    )
                )
                total_input += qusage.input_tokens
                total_output += qusage.output_tokens
                total_cost += qusage.cost_usd
            usage = UsageInfo(
                provider=p.name,
                model=p.model,
                input_tokens=total_input,
                output_tokens=total_output,
                cost_usd=round(total_cost, 6),
            )
            response = PageBlindResponse(
                provider=p.name,
                model=p.model,
                requested_at=started,
                query_results=results,
                usage=usage,
            )
            target.write_text(response.model_dump_json(indent=2), encoding="utf-8")
            mentions = sum(1 for r in results if r.brand_mentioned)
            console.print(
                f"    page_blind/{p.name} [green]ok[/] "
                f"[dim]({mentions}/{len(results)} queries mentioned brand)[/]"
            )
            return usage
        except Exception as exc:
            _write_failed(target, str(exc))
            console.print(f"    page_blind/{p.name} [red]failed[/]: {exc}")
            return None

    results = await asyncio.gather(*[_one_provider(p) for p in providers])
    return [u for u in results if u is not None]


# ============================================================================
# Verification (per page-aware response)
# ============================================================================


async def run_verification(
    run_dir: Path,
    url: str,
    providers: list[ProviderConfig],
    synthesis: SynthesisConfig,
    console: Console,
) -> list[UsageInfo]:
    rendered_html = (run_dir / "rendered_dom.html").read_text(encoding="utf-8")
    page_text = extract_visible_text(rendered_html)[:_PAGE_TEXT_CAP]
    prompt = load_prompt(prompts_dir() / "06_verification.md")

    async def _verify_one(p: ProviderConfig) -> UsageInfo | None:
        target = run_dir / "llm" / f"{p.name}_page_aware.json"
        if not target.exists():
            console.print(
                f"    verify/{p.name} [yellow]skipped[/] (no page_aware JSON)"
            )
            return None
        try:
            current = json.loads(target.read_text(encoding="utf-8"))
            if current.get("status") == "failed":
                console.print(
                    f"    verify/{p.name} [yellow]skipped[/] (page_aware failed)"
                )
                return None

            system, user = prompt.render(
                url=url,
                page_text=page_text,
                provider_response_json=json.dumps(current, indent=2),
            )
            client = _build_client(synthesis.provider, synthesis.model)
            parsed, usage = await with_retry(
                lambda: client.complete(
                    system=system,
                    user=user,
                    response_format=VerificationResult,
                    max_tokens=prompt.default_max_tokens,
                    temperature=prompt.default_temperature,
                ),
                op_name=f"verify/{p.name}",
            )
            assert isinstance(parsed, VerificationResult)
            current["hallucination_flags"] = json.loads(parsed.model_dump_json())
            target.write_text(json.dumps(current, indent=2), encoding="utf-8")
            n_hall = len(parsed.hallucinations)
            console.print(
                f"    verify/{p.name} [green]ok[/] "
                f"[dim](support={parsed.overall_support_level}, "
                f"hallucinations={n_hall})[/]"
            )
            return usage
        except Exception as exc:
            console.print(f"    verify/{p.name} [red]failed[/]: {exc}")
            return None

    results = await asyncio.gather(*[_verify_one(p) for p in providers])
    return [u for u in results if u is not None]


# ============================================================================
# Helpers
# ============================================================================


def _write_failed(path: Path, error: str) -> None:
    """Write a failure-marker JSON so the report can show partial results."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"status": "failed", "error": error}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
