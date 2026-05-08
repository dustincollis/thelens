"""Step 3 — Layer 1: site classification.

Single LLM call. Reads `rendered_dom.html` from the run folder, extracts
visible text and title, and asks the model to produce a `Classification`
fingerprint. Default model: Claude Opus.
"""

from __future__ import annotations

from pathlib import Path

from thelens.llm.anthropic_client import AnthropicClient
from thelens.llm.base import load_prompt
from thelens.models import Classification, UsageInfo
from thelens.pipeline._extract import extract_title, extract_visible_text


# Page text cap: ~100k chars ≈ 25k tokens. Enough for any reasonable single-page
# audit. Larger pages are truncated; downstream prompts see the truncated text.
_PAGE_TEXT_CAP = 100_000


async def classify(run_dir: Path, url: str) -> tuple[Classification, UsageInfo]:
    """Classify the site. Writes `classification.json` and returns the model + usage."""
    rendered_html = (run_dir / "rendered_dom.html").read_text(encoding="utf-8")
    page_text = extract_visible_text(rendered_html)[:_PAGE_TEXT_CAP]
    page_title = extract_title(rendered_html)

    prompt = load_prompt(Path.cwd() / "prompts" / "01_classification.md")
    system, user = prompt.render(url=url, page_title=page_title, page_text=page_text)

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
