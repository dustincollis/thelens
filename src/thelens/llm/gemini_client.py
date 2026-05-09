"""Gemini LLM client. Implements `LLMClient` from `llm/base.py`.

Uses `google-genai` SDK. Structured output is achieved by passing the
Pydantic class directly as `response_schema`; Gemini returns JSON that
the SDK auto-parses. Free-form text uses the same API without a schema.

Thinking budget is set to 0 by default — our prompts are well-structured
and don't need extended reasoning. This mirrors the OpenAI
`reasoning_effort=minimal` decision and keeps cost in line with the
posted token rates.
"""

from __future__ import annotations

import os

from google import genai
from google.genai import types as gtypes
from pydantic import BaseModel, ValidationError

from thelens.llm.base import LLMError
from thelens.models import UsageInfo


# Pricing per million tokens. Gemini 2.5 Pro has tiered pricing
# (cheaper ≤200k input tokens, more expensive >200k). We use the lower
# tier; with our 100-page corpus (~130k tokens) we never cross the line.
_PRICING_PER_M_TOKENS: dict[str, dict[str, float]] = {
    "gemini-2.5-pro":   {"input": 1.25, "output": 10.0, "cached_input": 0.31},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.5,  "cached_input": 0.075},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40, "cached_input": 0.025},
}


class GeminiClient:
    """Async-ish Gemini client conforming to `LLMClient`.

    The google-genai SDK exposes both sync and async APIs; we use the
    async one (`client.aio.models.generate_content`) for parity with the
    other clients.
    """

    provider_name = "gemini"

    def __init__(
        self,
        model: str = "gemini-2.5-pro",
        api_key: str | None = None,
    ) -> None:
        key = api_key or os.environ.get("GOOGLE_API_KEY") or os.environ.get(
            "GEMINI_API_KEY"
        )
        if not key:
            raise LLMError(
                self.provider_name,
                model,
                "GOOGLE_API_KEY (or GEMINI_API_KEY) is not set. "
                "Add it to .env or export it.",
            )
        self.model = model
        self._client = genai.Client(api_key=key)

    async def complete(
        self,
        system: str,
        user: str,
        response_format: type[BaseModel],
        max_tokens: int = 2048,
        temperature: float = 0.3,
        disable_web_search: bool = False,  # default no search; flag is no-op.
        cached_user_prefix: str | None = None,
    ) -> tuple[BaseModel, UsageInfo]:
        # Gemini's automatic prefix caching works similarly to OpenAI's:
        # consistent prefixes get a discount. We concatenate the cached
        # prefix at the start of `contents` so it sits at a stable
        # position; explicit cache management (caches.create) is heavier
        # weight and not necessary at our scale.
        contents = _build_contents(user, cached_user_prefix)
        config = gtypes.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=response_format,
            max_output_tokens=max_tokens,
            temperature=temperature,
            thinking_config=gtypes.ThinkingConfig(thinking_budget=0),
        )

        try:
            response = await self._client.aio.models.generate_content(
                model=self.model, contents=contents, config=config
            )
        except Exception as exc:
            raise LLMError(self.provider_name, self.model, f"API call failed: {exc}") from exc

        # The SDK auto-parses into the Pydantic class via `response.parsed`.
        # Fall back to manual parsing if `parsed` is None (rare but possible).
        parsed = getattr(response, "parsed", None)
        if parsed is None:
            text = (response.text or "").strip()
            try:
                import json as _json
                data = _json.loads(text)
                parsed = response_format.model_validate(data)
            except (ValueError, ValidationError) as exc:
                raise LLMError(
                    self.provider_name,
                    self.model,
                    f"could not parse response as {response_format.__name__}: {exc}\n"
                    f"first 300 chars: {text[:300]!r}",
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
        contents = _build_contents(user, cached_user_prefix)
        config = gtypes.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
            temperature=temperature,
            thinking_config=gtypes.ThinkingConfig(thinking_budget=0),
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self.model, contents=contents, config=config
            )
        except Exception as exc:
            raise LLMError(self.provider_name, self.model, f"API call failed: {exc}") from exc

        text = (response.text or "").strip()
        return text, _build_usage(self.provider_name, self.model, response)


def _build_contents(user: str, cached_user_prefix: str | None) -> str | list:
    """Construct the `contents` payload, splitting cached prefix when present."""
    if cached_user_prefix is None:
        return user
    # Two parts so the SDK keeps them as separate content blocks; consistent
    # prefix → automatic prefix caching applies.
    return [cached_user_prefix, user]


def _build_usage(provider: str, model: str, response: object) -> UsageInfo:
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return UsageInfo(
            provider=provider, model=model,
            input_tokens=0, output_tokens=0, cost_usd=0.0,
        )
    input_tokens = int(getattr(meta, "prompt_token_count", 0) or 0)
    output_tokens = int(getattr(meta, "candidates_token_count", 0) or 0)
    cached = int(getattr(meta, "cached_content_token_count", 0) or 0)
    return UsageInfo(
        provider=provider,
        model=model,
        input_tokens=input_tokens - cached,
        output_tokens=output_tokens,
        cost_usd=_compute_cost(model, input_tokens - cached, output_tokens, cached),
        cache_creation_tokens=0,  # Gemini doesn't bill creation separately
        cache_read_tokens=cached,
    )


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
