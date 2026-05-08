"""Step 4 — Layer 2: persona generation.

Single LLM call with the classification object as input. Produces 3–5
review personas, exactly one of which is the LLM-as-reader lens.
"""

from __future__ import annotations

from pathlib import Path

from thelens.llm.anthropic_client import AnthropicClient
from thelens.llm.base import load_prompt
from thelens.models import PersonaSet, UsageInfo


async def generate_personas(run_dir: Path) -> tuple[PersonaSet, UsageInfo]:
    """Generate personas from `classification.json`. Writes `personas.json`."""
    classification_json = (run_dir / "classification.json").read_text(encoding="utf-8")

    prompt = load_prompt(Path.cwd() / "prompts" / "02_persona_generation.md")
    system, user = prompt.render(classification_json=classification_json)

    client = AnthropicClient()
    parsed, usage = await client.complete(
        system=system,
        user=user,
        response_format=PersonaSet,
        max_tokens=prompt.default_max_tokens,
        temperature=prompt.default_temperature,
    )

    assert isinstance(parsed, PersonaSet)
    (run_dir / "personas.json").write_text(
        parsed.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return parsed, usage
