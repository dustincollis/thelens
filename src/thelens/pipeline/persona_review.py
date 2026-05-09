"""Step 7 — Layer 4: per-persona reviews.

For each persona in `personas.json`, one LLM call has the model roleplay
as that persona and review the page. Output is one
`persona_reviews/persona_<n>.json` per persona.

Sequential rather than parallel: with one synthesis-grade model and 3-5
personas this is ~30s total, and serializing keeps us comfortably within
provider rate limits.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from thelens.config import SynthesisConfig, prompts_dir
from thelens.llm.base import load_prompt
from thelens.llm.factory import build_client
from thelens.llm.retry import with_retry
from thelens.models import Persona, PersonaReview, PersonaSet, UsageInfo
from thelens.pipeline._extract import extract_title, extract_visible_text


_PAGE_TEXT_CAP = 100_000


async def review_one_persona(
    run_dir: Path,
    url: str,
    persona: Persona,
    persona_index: int,
    synthesis: SynthesisConfig,
) -> tuple[PersonaReview, UsageInfo]:
    rendered_html = (run_dir / "rendered_dom.html").read_text(encoding="utf-8")
    page_text = extract_visible_text(rendered_html)[:_PAGE_TEXT_CAP]
    page_title = extract_title(rendered_html)

    prompt = load_prompt(prompts_dir() / "04_persona_review.md")
    system, user = prompt.render(
        persona_json=persona.model_dump_json(indent=2),
        url=url,
        page_title=page_title,
        page_text=page_text,
    )

    client = build_client(synthesis.provider, synthesis.model)
    parsed, usage = await with_retry(
        lambda: client.complete(
            system=system,
            user=user,
            response_format=PersonaReview,
            max_tokens=prompt.default_max_tokens,
            temperature=prompt.default_temperature,
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

    usages: list[UsageInfo] = []
    for i, persona in enumerate(persona_set.personas, start=1):
        review, usage = await review_one_persona(
            run_dir, url, persona, i, synthesis
        )
        target = run_dir / "persona_reviews" / f"persona_{i}.json"
        target.write_text(review.model_dump_json(indent=2), encoding="utf-8")
        console.print(
            f"    persona_review/{i} ({persona.name}) [green]ok[/] "
            f"[dim](goal={review.goal_outcome}, "
            f"score={review.persona_satisfaction_score})[/]"
        )
        usages.append(usage)
    return usages
