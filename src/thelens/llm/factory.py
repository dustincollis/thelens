"""Provider-name → LLMClient factory.

Centralized so every pipeline step that builds a client uses the same
construction path. Adding a new provider = one new branch here plus the
client class itself.
"""

from __future__ import annotations

from thelens.llm.anthropic_client import AnthropicClient
from thelens.llm.base import LLMClient


def build_client(name: str, model: str) -> LLMClient:
    if name == "anthropic":
        return AnthropicClient(model=model)
    raise ValueError(
        f"unknown / not-yet-implemented provider: {name!r}. "
        "Add a client class in src/thelens/llm/ and a branch here."
    )
