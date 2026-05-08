"""Project config loaders and dynamic schema builder.

Reads `config/questions.yaml` and `config/models.yaml`. The dynamic
`PageAwareAnswers` schema is built from the question list at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, create_model

from thelens.models import (
    BooleanWithExplanation,
    Question,
    ScoreAnswer,
)


# ============================================================================
# Project paths
# ============================================================================


def project_root() -> Path:
    """Project root = current working directory.

    `uv run lens ...` invokes us from the project root by default. Path-using
    helpers below resolve relative to whatever this returns.
    """
    return Path.cwd()


def config_dir() -> Path:
    return project_root() / "config"


def prompts_dir() -> Path:
    return project_root() / "prompts"


# ============================================================================
# Questions config
# ============================================================================


def load_questions(path: Path | None = None) -> list[Question]:
    p = path or (config_dir() / "questions.yaml")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    raw = data.get("questions") or []
    return [Question.model_validate(q) for q in raw]


def build_page_aware_answers_model(questions: list[Question]) -> type[BaseModel]:
    """Construct a Pydantic v2 model with one field per question id.

    Field types map to the question type:
      text                     → str
      list                     → list[str]
      score                    → ScoreAnswer
      boolean_with_explanation → BooleanWithExplanation

    The model uses `extra="forbid"` so any field not in `questions` is
    rejected — keeps the LLM output strictly conforming to the configured set.
    """
    fields: dict[str, Any] = {}
    for q in questions:
        if q.type == "text":
            field_type: Any = str
        elif q.type == "list":
            field_type = list[str]
        elif q.type == "score":
            field_type = ScoreAnswer
        elif q.type == "boolean_with_explanation":
            field_type = BooleanWithExplanation
        else:
            raise ValueError(f"unknown question type: {q.type!r}")
        fields[q.id] = (field_type, Field(...))

    return create_model(
        "PageAwareAnswers",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )


# ============================================================================
# Models config (providers, synthesis, budget)
# ============================================================================


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    model: str
    enabled: bool
    max_concurrent: int


@dataclass(frozen=True)
class SynthesisConfig:
    provider: str
    model: str


@dataclass(frozen=True)
class ModelsConfig:
    providers: list[ProviderConfig]
    synthesis: SynthesisConfig
    budget_usd: float

    def enabled_providers(self) -> list[ProviderConfig]:
        return [p for p in self.providers if p.enabled]


def load_models_config(path: Path | None = None) -> ModelsConfig:
    p = path or (config_dir() / "models.yaml")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    providers_raw = data.get("providers") or {}
    providers = [
        ProviderConfig(
            name=name,
            model=str(spec.get("model", "")),
            enabled=bool(spec.get("enabled", False)),
            max_concurrent=int(spec.get("max_concurrent", 4)),
        )
        for name, spec in providers_raw.items()
    ]

    synthesis_raw = data.get("synthesis") or {}
    synthesis = SynthesisConfig(
        provider=str(synthesis_raw.get("provider", "anthropic")),
        model=str(synthesis_raw.get("model", "claude-opus-4-7")),
    )

    return ModelsConfig(
        providers=providers,
        synthesis=synthesis,
        budget_usd=float(data.get("budget_usd", 0.0)),
    )
