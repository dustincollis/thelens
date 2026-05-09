"""Step 7 — Layer 4: per-persona site reviews.

For each persona in `personas.json`, one LLM call has the model roleplay
as that persona and review the SITE (multi-page corpus). Output is one
`persona_reviews/persona_<n>.json` per persona.

Sequential rather than parallel: with one synthesis-grade model and 3-5
personas this is ~30-60s total, and serializing keeps us comfortably
within provider rate limits.

Prompt caching: the rendered user prompt is split on a `<!-- CACHE_BREAK -->`
marker so the corpus-bearing prefix is sent as a cached block. With 5
personas in a row, calls 2-5 read the cached prefix at 0.1x the per-token
cost — meaningful at 100-page corpus sizes.
"""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console

from thelens.config import SynthesisConfig, prompts_dir
from thelens.llm.base import load_prompt
from thelens.llm.factory import build_client
from thelens.llm.retry import with_retry
from thelens.models import Persona, PersonaReview, PersonaSet, UsageInfo
from thelens.pipeline.corpus import (
    build_site_corpus,
    homepage_title,
    homepage_url,
)


_CACHE_BREAK = "<!-- CACHE_BREAK -->"


def _split_on_cache_break(rendered: str) -> tuple[str | None, str]:
    if _CACHE_BREAK not in rendered:
        return None, rendered
    cached, _, rest = rendered.partition(_CACHE_BREAK)
    return cached.strip(), rest.strip()


async def review_one_persona(
    run_dir: Path,
    url: str,
    persona: Persona,
    persona_index: int,
    synthesis: SynthesisConfig,
    site_text: str,
    site_title: str,
    site_url: str,
) -> tuple[PersonaReview, UsageInfo]:
    prompt = load_prompt(prompts_dir() / "04_persona_review.md")
    system, user = prompt.render(
        persona_json=persona.model_dump_json(indent=2),
        site_url=site_url,
        site_title=site_title,
        site_text=site_text,
    )
    cached_prefix, user_rest = _split_on_cache_break(user)

    client = build_client(synthesis.provider, synthesis.model)
    parsed, usage = await with_retry(
        lambda: client.complete(
            system=system,
            user=user_rest,
            response_format=PersonaReview,
            max_tokens=prompt.default_max_tokens,
            temperature=prompt.default_temperature,
            cached_user_prefix=cached_prefix,
        ),
        op_name=f"persona_review/{persona_index}",
    )
    assert isinstance(parsed, PersonaReview)
    return parsed, usage


async def run_persona_reviews(
    run_dir: Path,
    url: str,
    synthesis: SynthesisConfig,
    console: Console,
) -> list[UsageInfo]:
    persona_set = PersonaSet.model_validate_json(
        (run_dir / "personas.json").read_text(encoding="utf-8")
    )

    # Build the corpus once; reuse across all persona reviews.
    site_text = build_site_corpus(run_dir)
    site_title = homepage_title(run_dir)
    site_url = homepage_url(run_dir) or url

    # Per-persona failures are non-fatal: we record a failure marker for the
    # persona and continue. Synthesis still runs against the surviving
    # reviews. This avoids torching everything spent earlier in the pipeline
    # because of one flaky LLM call.
    usages: list[UsageInfo] = []
    for i, persona in enumerate(persona_set.personas, start=1):
        target = run_dir / "persona_reviews" / f"persona_{i}.json"
        try:
            review, usage = await review_one_persona(
                run_dir, url, persona, i, synthesis,
                site_text=site_text, site_title=site_title, site_url=site_url,
            )
        except Exception as exc:
            target.write_text(
                json.dumps(
                    {
                        "status": "failed",
                        "persona_name": persona.name,
                        "persona_role": persona.role,
                        "error": str(exc),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            console.print(
                f"    persona_review/{i} ({persona.name}) [red]failed[/]: {exc}"
            )
            continue

        target.write_text(review.model_dump_json(indent=2), encoding="utf-8")
        cache_note = ""
        if usage.cache_read_tokens:
            cache_note = f" [dim](cached {usage.cache_read_tokens} tok)[/]"
        elif usage.cache_creation_tokens:
            cache_note = f" [dim](cache+{usage.cache_creation_tokens} tok)[/]"
        console.print(
            f"    persona_review/{i} ({persona.name}) [green]ok[/] "
            f"[dim](goal={review.goal_outcome}, "
            f"score={review.persona_satisfaction_score})[/]{cache_note}"
        )
        usages.append(usage)
    return usages
