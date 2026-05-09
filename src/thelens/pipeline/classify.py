"""Step 3 — Layer 1: site classification (multi-page corpus aware).

Single LLM call. Reads the cross-page site corpus produced by `corpus.py`
and asks the model to produce a `Classification` fingerprint for the site.
"""

from __future__ import annotations

from pathlib import Path

from thelens.llm.anthropic_client import AnthropicClient
from thelens.llm.base import load_prompt
from thelens.config import prompts_dir
from thelens.models import Classification, UsageInfo
from thelens.pipeline.corpus import (
    build_site_corpus,
    homepage_title,
    homepage_url,
)


async def classify(run_dir: Path, url: str) -> tuple[Classification, UsageInfo]:
    site_text = build_site_corpus(run_dir)
    site_title = homepage_title(run_dir)
    site_url = homepage_url(run_dir) or url

    prompt = load_prompt(prompts_dir() / "01_classification.md")
    system, user = prompt.render(
        site_url=site_url, site_title=site_title, site_text=site_text
    )

    client = AnthropicClient()
    parsed, usage = await client.complete(
        system=system,
        user=user,
        response_format=Classification,
        max_tokens=prompt.default_max_tokens,
        temperature=prompt.default_temperature,
    )

    assert isinstance(parsed, Classification)
    (run_dir / "classification.json").write_text(
        parsed.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return parsed, usage
