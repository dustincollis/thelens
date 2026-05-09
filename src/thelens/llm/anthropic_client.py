"""Anthropic LLM client. Implements `LLMClient` from `llm/base.py`.

Uses Anthropic's forced tool-use to get strictly structured output: a single
synthetic tool is registered whose `input_schema` is the JSON Schema of the
`response_format` Pydantic model, and the model is forced to call it via
`tool_choice`. Output comes back as the tool's `input` dict, which we then
validate.

Prompt caching: when a caller passes `cached_user_prefix`, that text is
sent as a separate user content block with `cache_control: ephemeral`.
Useful for steps that make many sequential calls reusing the same large
preamble (e.g. persona reviews — same page text across N personas). The
returned `UsageInfo` populates `cache_creation_tokens` / `cache_read_tokens`
and `cost_usd` includes Anthropic's cache surcharges.
"""

from __future__ import annotations

import os

from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError

from thelens.llm.base import LLMError
from thelens.models import UsageInfo


def _tool_name_for(model_cls: type) -> str:
    return f"submit_{model_cls.__name__.lower()}"


# Pricing per million tokens. Update as model pricing changes.
# Source: anthropic.com/pricing as of model release.
_PRICING_PER_M_TOKENS: dict[str, dict[str, float]] = {
    "claude-opus-4-7": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
}

# Anthropic prompt-cache pricing multipliers, applied to base input rate.
_CACHE_WRITE_MULTIPLIER = 1.25
_CACHE_READ_MULTIPLIER = 0.1


class AnthropicClient:
    """Async Anthropic client conforming to `LLMClient`."""

    provider_name = "anthropic"

    def __init__(
        self,
        model: str = "claude-opus-4-7",
        api_key: str | None = None,
    ) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError(
                self.provider_name,
                model,
                "ANTHROPIC_API_KEY is not set. Add it to .env or export it.",
            )
        self.model = model
        self._client = AsyncAnthropic(api_key=key)

    async def complete(
        self,
        system: str,
        user: str,
        response_format: type[BaseModel],
        max_tokens: int = 2048,
        temperature: float = 0.3,
        disable_web_search: bool = False,  # Anthropic default: no search; flag is no-op here.
        cached_user_prefix: str | None = None,
    ) -> tuple[BaseModel, UsageInfo]:
        tool_name = _tool_name_for(response_format)
        tool = {
            "name": tool_name,
            "description": (
                f"Record the {response_format.__name__} for this analysis. "
                "Pass each schema field as a top-level argument to this tool. "
                "Do NOT wrap the fields in an outer object."
            ),
            "input_schema": _to_anthropic_schema(response_format),
        }

        request_kwargs: dict[str, object] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": _build_messages(user, cached_user_prefix),
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": tool_name},
        }
        if not _model_rejects_temperature(self.model):
            request_kwargs["temperature"] = temperature

        try:
            response = await self._client.messages.create(**request_kwargs)
        except Exception as exc:
            raise LLMError(self.provider_name, self.model, f"API call failed: {exc}") from exc

        data = _extract_tool_input(response)
        if data is None:
            raise LLMError(
                self.provider_name,
                self.model,
                "no tool_use block in response (expected forced tool call)",
            )

        # Defensive unwrap: occasionally the model wraps the response under a
        # single key matching the type name (e.g. {"personaset": {...}}). Try
        # the literal payload first, fall back to unwrapping if it fails.
        candidates: list[dict] = [data]
        if len(data) == 1:
            only_value = next(iter(data.values()))
            if isinstance(only_value, dict):
                candidates.append(only_value)

        last_err: Exception | None = None
        parsed: BaseModel | None = None
        for candidate in candidates:
            try:
                parsed = response_format.model_validate(candidate)
                break
            except ValidationError as exc:
                last_err = exc
        if parsed is None:
            raise LLMError(
                self.provider_name,
                self.model,
                f"tool input did not match {response_format.__name__}:\n{last_err}",
            )

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
        request_kwargs: dict[str, object] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": _build_messages(user, cached_user_prefix),
        }
        if not _model_rejects_temperature(self.model):
            request_kwargs["temperature"] = temperature

        try:
            response = await self._client.messages.create(**request_kwargs)
        except Exception as exc:
            raise LLMError(self.provider_name, self.model, f"API call failed: {exc}") from exc

        text = _extract_text_blocks(response)
        return text, _build_usage(self.provider_name, self.model, response)


def _build_messages(user: str, cached_user_prefix: str | None) -> list[dict]:
    """Construct the messages array, adding a cached prefix block if given."""
    if cached_user_prefix is None:
        return [{"role": "user", "content": user}]
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": cached_user_prefix,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": user},
            ],
        }
    ]


def _build_usage(provider: str, model: str, response: object) -> UsageInfo:
    """Read token counts from the SDK response, including cache fields."""
    u = response.usage  # type: ignore[attr-defined]
    cache_create = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
    cache_read = int(getattr(u, "cache_read_input_tokens", 0) or 0)
    return UsageInfo(
        provider=provider,
        model=model,
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        cost_usd=_compute_cost(
            model, u.input_tokens, u.output_tokens, cache_create, cache_read
        ),
        cache_creation_tokens=cache_create,
        cache_read_tokens=cache_read,
    )


def _extract_text_blocks(response: object) -> str:
    """Concatenate text blocks from a (non-tool-use) Messages response."""
    blocks = getattr(response, "content", None) or []
    parts: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts).strip()


def _extract_tool_input(response: object) -> dict | None:
    """Find the first `tool_use` block and return its `input` dict."""
    blocks = getattr(response, "content", None) or []
    for block in blocks:
        if getattr(block, "type", None) == "tool_use":
            data = getattr(block, "input", None)
            if isinstance(data, dict):
                return data
    return None


def _to_anthropic_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON Schema → Anthropic tool input_schema.

    Strip top-level metadata that can confuse the model into wrapping its
    output (a top-level `description` like "Output of step 3..." sometimes
    leads the model to nest the response under a `response` key).
    """
    schema = model.model_json_schema()
    for key in ("$schema", "title", "description"):
        schema.pop(key, None)
    return schema


_MODELS_WITHOUT_TEMPERATURE = {"claude-opus-4-7"}


def _model_rejects_temperature(model: str) -> bool:
    """Some newer Claude models hard-code sampling and reject `temperature`."""
    return model in _MODELS_WITHOUT_TEMPERATURE


def _compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    p = _PRICING_PER_M_TOKENS.get(model)
    if not p:
        return 0.0
    cost = (
        input_tokens * p["input"]
        + output_tokens * p["output"]
        + cache_creation_tokens * p["input"] * _CACHE_WRITE_MULTIPLIER
        + cache_read_tokens * p["input"] * _CACHE_READ_MULTIPLIER
    )
    return cost / 1_000_000
