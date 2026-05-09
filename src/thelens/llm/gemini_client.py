"""Gemini LLM client. Implements `LLMClient` from `llm/base.py`.

Uses `google-genai` SDK. Structured output is achieved by passing the
Pydantic class directly as `response_schema`; Gemini returns JSON that
the SDK auto-parses. Free-form text uses the same API without a schema.

Thinking budget is set to -1 (dynamic — model decides) since Gemini 2.5
Pro rejects thinking_budget=0. Most calls will use a small amount of
reasoning. If costs grow unexpectedly, this is the first knob to lower.
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
        # prefix at the start of `contents` so it sits at a stable position.
        contents = _build_contents(user, cached_user_prefix)

        # Build a Gemini-compatible schema dict instead of passing the
        # Pydantic class directly. The auto-conversion path emits
        # `additionalProperties` which Gemini rejects; we strip it (and
        # other unsupported metadata) and inline `$ref` pointers.
        schema_dict = _to_gemini_schema(response_format)

        config = gtypes.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema_dict,
            max_output_tokens=max_tokens,
            temperature=temperature,
            thinking_config=gtypes.ThinkingConfig(thinking_budget=-1),
        )

        try:
            response = await self._client.aio.models.generate_content(
                model=self.model, contents=contents, config=config
            )
        except Exception as exc:
            raise LLMError(self.provider_name, self.model, f"API call failed: {exc}") from exc

        # We pass a dict schema, not a Pydantic class, so response.parsed
        # isn't auto-populated. Parse + validate manually.
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
            thinking_config=gtypes.ThinkingConfig(thinking_budget=-1),
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self.model, contents=contents, config=config
            )
        except Exception as exc:
            raise LLMError(self.provider_name, self.model, f"API call failed: {exc}") from exc

        text = (response.text or "").strip()
        return text, _build_usage(self.provider_name, self.model, response)


def _to_gemini_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON Schema → Gemini-compatible response_schema dict.

    Gemini's structured-output schema spec is OpenAPI-3.0-derived and
    rejects several keywords Pydantic emits by default:
      - `additionalProperties` — not in Gemini's spec
      - `$schema`, `$defs`, `$ref` — must be inlined
      - `title` — sometimes accepted, sometimes not, safer to strip
      - `default` — Gemini ignores; harmless but noisy

    This helper inlines refs from `$defs` and recursively strips the
    unsupported keys.
    """
    schema = model.model_json_schema()
    schema.pop("$schema", None)
    defs = schema.pop("$defs", {})
    schema = _inline_refs(schema, defs)
    _strip_keys(schema, {"additionalProperties", "title", "default"})
    return schema


def _inline_refs(node: object, defs: dict) -> object:
    """Replace `$ref` pointers with the referenced subschema."""
    if isinstance(node, dict):
        if "$ref" in node:
            ref = node["$ref"]  # e.g. "#/$defs/ContentMaturity"
            name = ref.split("/")[-1]
            return _inline_refs(defs.get(name, {}), defs)
        return {k: _inline_refs(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_refs(x, defs) for x in node]
    return node


def _strip_keys(node: object, keys: set[str]) -> None:
    """Remove specified keys recursively in-place."""
    if isinstance(node, dict):
        for k in keys:
            node.pop(k, None)
        for v in node.values():
            _strip_keys(v, keys)
    elif isinstance(node, list):
        for x in node:
            _strip_keys(x, keys)


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
