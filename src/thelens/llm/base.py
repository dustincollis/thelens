"""LLM client Protocol and shared helpers (prompt loader, errors).

Every provider implements `LLMClient`. The Protocol shape is fixed in
CLAUDE.md and SPEC §6. Phase 2 ships only the Anthropic client; later
phases add OpenAI (Phase 3) and Gemini/Grok (deferred — see SPEC §16).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml
from jinja2 import Template
from pydantic import BaseModel

from thelens.models import UsageInfo


class LLMError(Exception):
    """Wraps any provider-level failure with provider/model context."""

    def __init__(self, provider: str, model: str, message: str):
        super().__init__(f"[{provider}/{model}] {message}")
        self.provider = provider
        self.model = model


class LLMClient(Protocol):
    """Provider-agnostic LLM client interface.

    Implementations MUST return JSON parsed and validated against
    `response_format`. They MUST also return a `UsageInfo` populated with
    real token counts and a computed cost in USD.
    """

    provider_name: str
    model: str

    async def complete(
        self,
        system: str,
        user: str,
        response_format: type[BaseModel],
        max_tokens: int = 2048,
        temperature: float = 0.3,
        disable_web_search: bool = False,
    ) -> tuple[BaseModel, UsageInfo]: ...

    async def complete_text(
        self,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.3,
        disable_web_search: bool = False,
    ) -> tuple[str, UsageInfo]:
        """Free-form text completion. Used for page-blind queries where we
        want the model's natural prose answer, not structured JSON."""
        ...


# ============================================================================
# Prompt loader
# ============================================================================


@dataclass(frozen=True)
class PromptTemplate:
    """Parsed prompt file: frontmatter metadata + Jinja-rendered system/user bodies.

    Frontmatter keys:
      - name (str)
      - description (str)
      - default_provider (str)        — e.g. "anthropic"
      - default_temperature (float)
      - default_max_tokens (int)
      - output_schema (str, informational)

    Body has two H1 sections:
      `# System`  → system prompt
      `# User`    → user prompt, may contain Jinja {{ variable }} placeholders
    """

    name: str
    description: str
    default_provider: str
    default_temperature: float
    default_max_tokens: int
    output_schema: str
    system_prompt: str
    user_template: str

    def render(self, **kwargs: object) -> tuple[str, str]:
        """Return `(system, user)` with Jinja variables substituted."""
        return self.system_prompt, Template(self.user_template).render(**kwargs)


_FRONTMATTER_RE = re.compile(r"\A---\n(?P<yaml>.*?)\n---\n(?P<body>.*)\Z", re.DOTALL)
_SECTION_RE = re.compile(
    r"^#\s+System\s*\n(?P<system>.*?)^#\s+User\s*\n(?P<user>.*)\Z",
    re.DOTALL | re.MULTILINE,
)


def load_prompt(path: Path) -> PromptTemplate:
    text = path.read_text(encoding="utf-8")

    fm = _FRONTMATTER_RE.match(text)
    if not fm:
        raise ValueError(
            f"Prompt {path} is missing YAML frontmatter. "
            "Expected `---`-delimited block at the top of the file."
        )
    meta = yaml.safe_load(fm["yaml"]) or {}

    body = _SECTION_RE.search(fm["body"])
    if not body:
        raise ValueError(
            f"Prompt {path} is missing `# System` or `# User` section."
        )

    return PromptTemplate(
        name=meta["name"],
        description=meta.get("description", ""),
        default_provider=meta.get("default_provider", "anthropic"),
        default_temperature=float(meta.get("default_temperature", 0.3)),
        default_max_tokens=int(meta.get("default_max_tokens", 2048)),
        output_schema=meta.get("output_schema", ""),
        system_prompt=body["system"].strip(),
        user_template=body["user"].strip(),
    )
