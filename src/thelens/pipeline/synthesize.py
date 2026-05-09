"""Step 8 — Layer 5: cross-lens synthesis.

One LLM call. Reads every prior artifact in the run folder (technical
audit, classification, personas, all multi-LLM responses with their
verifications, all persona reviews) and produces a single Synthesis with
convergence findings, divergence findings, prioritized recommendations,
a per-dimension score breakdown, and a composite score (0-100).

The composite score is also pushed back into the RunManifest so it can
be queried via SQLite and surfaced in the dashboard later.
"""

from __future__ import annotations

import json
from pathlib import Path

from thelens.config import ProviderConfig, SynthesisConfig, prompts_dir
from thelens.llm.base import load_prompt
from thelens.llm.factory import build_client
from thelens.llm.retry import with_retry
from thelens.models import Synthesis, UsageInfo
from thelens.pipeline.corpus import build_audit_summary, homepage_url


async def run_synthesis(
    run_dir: Path,
    url: str,
    providers: list[ProviderConfig],
    synthesis: SynthesisConfig,
) -> tuple[Synthesis, UsageInfo]:
    technical_audit = json.dumps(build_audit_summary(run_dir), indent=2)
    classification = (run_dir / "classification.json").read_text(encoding="utf-8")
    personas = (run_dir / "personas.json").read_text(encoding="utf-8")
    site_url = homepage_url(run_dir) or url

    page_aware: dict[str, object] = {}
    page_blind: dict[str, object] = {}
    for p in providers:
        pa = run_dir / "llm" / f"{p.name}_page_aware.json"
        pb = run_dir / "llm" / f"{p.name}_page_blind.json"
        if pa.exists():
            page_aware[f"{p.name}_page_aware"] = json.loads(
                pa.read_text(encoding="utf-8")
            )
        if pb.exists():
            page_blind[f"{p.name}_page_blind"] = json.loads(
                pb.read_text(encoding="utf-8")
            )

    review_dir = run_dir / "persona_reviews"
    persona_reviews: dict[str, object] = {}
    for f in sorted(review_dir.glob("persona_*.json")):
        persona_reviews[f.stem] = json.loads(f.read_text(encoding="utf-8"))

    prompt = load_prompt(prompts_dir() / "05_synthesis.md")
    system, user = prompt.render(
        site_url=site_url,
        technical_audit_json=technical_audit,
        classification_json=classification,
        personas_json=personas,
        page_aware_responses_json=json.dumps(page_aware, indent=2),
        page_blind_responses_json=json.dumps(page_blind, indent=2),
        persona_reviews_json=json.dumps(persona_reviews, indent=2),
    )

    client = build_client(synthesis.provider, synthesis.model)
    parsed, usage = await with_retry(
        lambda: client.complete(
            system=system,
            user=user,
            response_format=Synthesis,
            max_tokens=prompt.default_max_tokens,
            temperature=prompt.default_temperature,
        ),
        op_name="synthesis",
    )
    assert isinstance(parsed, Synthesis)
    (run_dir / "synthesis.json").write_text(
        parsed.model_dump_json(indent=2), encoding="utf-8"
    )
    return parsed, usage
