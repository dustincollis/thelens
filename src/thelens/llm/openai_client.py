"""OpenAI LLM client. Implements `LLMClient` from `llm/base.py`.

Uses the chat-completions endpoint with `response_format=json_schema` and
`strict: true` for structured output (the OpenAI equivalent of Anthropic's
forced tool use). Free-form text completion (page-blind queries) uses the
same endpoint without `response_format`.

Prompt caching: OpenAI caches automatically for prompts ≥1024 tokens —
no explicit markers needed. The `cached_user_prefix` parameter is
accepted for protocol compatibility; it's structured as the first user
content block so it sits at a stable position in the prefix, but it
doesn't change billing on its own.
"""

from __future__ import annotations

import os

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from thelens.llm.base import LLMError
from thelens.models import UsageInfo


# Pricing per million tokens. Rough estimates as of model release; update
# as needed. Cached input is ~50% of normal input (OpenAI's posted ratio).
_PRICING_PER_M_TOKENS: dict[str, dict[str, float]] = {
    "gpt-5":      {"input": 1.25, "output": 10.0, "cached_input": 0.125},
    "gpt-5-mini": {"input": 0.25, "output": 2.0,  "cached_input": 0.025},
    "gpt-4.1":    {"input": 2.0,  "output": 8.0,  "cached_input": 0.5},
    "gpt-4o":     {"input": 2.5,  "output": 10.0, "cached_input": 1.25},
}


class OpenAIClient:
    """Async OpenAI client conforming to `LLMClient`."""

    provider_name = "openai"

    def __init__(
        self,
        model: str = "gpt-5",
        api_key: str | None = None,
    ) -> None:
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise LLMError(
                self.provider_name,
                model,
                "OPENAI_API_KEY is not set. Add it to .env or export it.",
            )
        self.model = model
        self._client = AsyncOpenAI(api_key=key)

    async def complete(
        self,
        system: str,
        user: str,
        response_format: type[BaseModel],
        max_tokens: int = 2048,
        temperature: float = 0.3,
        disable_web_search: bool = False,  # chat-completions doesn't search; no-op.
        cached_user_prefix: str | None = None,
    ) -> tuple[BaseModel, UsageInfo]:
        schema = _to_openai_schema(response_format)
        messages = _build_messages(system, user, cached_user_prefix)

        kwargs: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": response_format.__name__,
                    "schema": schema,
                    "strict": True,
                },
            },
            "max_completion_tokens": max_tokens,
        }
        # GPT-5 family rejects `temperature` (only the default 1.0 is allowed).
        if not _model_rejects_temperature(self.model):
            kwargs["temperature"] = temperature

        try:
            response = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            raise LLMError(self.provider_name, self.model, f"API call failed: {exc}") from exc

        text = (response.choices[0].message.content or "").strip()
        try:
            import json as _json
            data = _json.loads(text)
        except Exception as exc:
            raise LLMError(
                self.provider_name,
                self.model,
                f"response was not valid JSON: {exc}\nfirst 300 chars: {text[:300]!r}",
            ) from exc

        try:
            parsed = response_format.model_validate(data)
        except ValidationError as exc:
            raise LLMError(
                self.provider_name,
                self.model,
                f"JSON did not match {response_format.__name__}:\n{exc}",
            ) from exc

        return parsed, _build_usage(self.provider_name, self.model, response)

    async def complete_text(
        self,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.3,
        disable_web_search: bool = False,
        cached_user_prefix: str | None = None,
    ) -> tuple[str, UsageInfo]:
        messages = _build_messages(system, user, cached_user_prefix)
        kwargs: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "max_completion_tokens": max_tokens,
        }
        if not _model_rejects_temperature(self.model):
            kwargs["temperature"] = temperature

        try:
            response = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            raise LLMError(self.provider_name, self.model, f"API call failed: {exc}") from exc

        text = (response.choices[0].message.content or "").strip()
        return text, _build_usage(self.provider_name, self.model, response)


def _build_messages(
    system: str, user: str, cached_user_prefix: str | None
) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": system}]
    if cached_user_prefix:
        # Two separate user messages so the prefix is at a stable position
        # in the prefix-match cache key.
        msgs.append({"role": "user", "content": cached_user_prefix})
        msgs.append({"role": "user", "content": user})
    else:
        msgs.append({"role": "user", "content": user})
    return msgs


def _build_usage(provider: str, model: str, response: object) -> UsageInfo:
    """Read token counts from the SDK response, including prompt-cache fields."""
    u = response.usage  # type: ignore[attr-defined]
    input_tokens = int(getattr(u, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(u, "completion_tokens", 0) or 0)
    # Cached prompt tokens are exposed under prompt_tokens_details.cached_tokens
    cached = 0
    details = getattr(u, "prompt_tokens_details", None)
    if details is not None:
        cached = int(getattr(details, "cached_tokens", 0) or 0)

    cost = _compute_cost(model, input_tokens, output_tokens, cached)
    return UsageInfo(
        provider=provider,
        model=model,
        input_tokens=input_tokens - cached,  # uncached portion
        output_tokens=output_tokens,
        cost_usd=cost,
        cache_creation_tokens=0,  # OpenAI doesn't bill creation separately
        cache_read_tokens=cached,
    )


def _to_openai_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON Schema → OpenAI structured-output schema.

    OpenAI's strict mode requires:
      - `additionalProperties: false` on every object (Pydantic with
        `extra='forbid'` already does this)
      - All properties in `required`
      - No top-level `title`, `description`, `$schema`
      - Nullable fields must use `["type", "null"]` instead of just type
    Pydantic's output mostly satisfies these except for `required` (which
    Pydantic only includes for non-default fields). We force every property
    into `required` for strict compatibility.
    """
    schema = model.model_json_schema()
    for key in ("$schema", "title", "description"):
        schema.pop(key, None)
    _enforce_strict(schema)
    return schema


def _enforce_strict(node: object) -> None:
    """Recursively walk a JSON Schema and make it strict-mode compatible."""
    if isinstance(node, dict):
        # OpenAI strict mode requires every property to be in `required`.
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"].keys())
        # Strip metadata that strict mode doesn't accept on $defs entries.
        for key in ("title",):
            node.pop(key, None)
        for value in node.values():
            _enforce_strict(value)
    elif isinstance(node, list):
        for item in node:
            _enforce_strict(item)


_MODELS_REJECTING_TEMPERATURE = {"gpt-5", "gpt-5-mini", "gpt-5-nano"}


def _model_rejects_temperature(model: str) -> bool:
    """GPT-5 family hard-codes sampling and rejects non-default temperature."""
    return model in _MODELS_REJECTING_TEMPERATURE


def _compute_cost(
    model: str,
    input_tokens_uncached: int,
    output_tokens: int,
    cached_tokens: int = 0,
) -> float:
    p = _PRICING_PER_M_TOKENS.get(model)
    if not p:
        return 0.0
    cost = (
        input_tokens_uncached * p["input"]
        + cached_tokens * p.get("cached_input", p["input"])
        + output_tokens * p["output"]
    )
    return cost / 1_000_000
