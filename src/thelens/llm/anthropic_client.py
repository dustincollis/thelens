"""Anthropic LLM client. Implements `LLMClient` from `llm/base.py`.

Uses Anthropic's forced tool-use to get strictly structured output: a single
synthetic tool is registered whose `input_schema` is the JSON Schema of the
`response_format` Pydantic model, and the model is forced to call it via
`tool_choice`. Output comes back as the tool's `input` dict, which we then
validate.
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
            "messages": [{"role": "user", "content": user}],
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": tool_name},
        }
        # Opus 4.7+ rejects `temperature`. Older models still accept it.
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

        try:
            parsed = response_format.model_validate(data)
        except ValidationError as exc:
            raise LLMError(
                self.provider_name,
                self.model,
                f"tool input did not match {response_format.__name__}:\n{exc}",
            ) from exc

        usage = UsageInfo(
            provider=self.provider_name,
            model=self.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=_compute_cost(
                self.model, response.usage.input_tokens, response.usage.output_tokens
            ),
        )
        return parsed, usage


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


def _compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    p = _PRICING_PER_M_TOKENS.get(model)
    if not p:
        return 0.0
    return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000
