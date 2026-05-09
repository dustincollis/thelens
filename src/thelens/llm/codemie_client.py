"""CodeMie LLM client. Implements `LLMClient` from `llm/base.py`.

CodeMie is EPAM's internal AI gateway. It exposes an "assistants" HTTP
API rather than an OpenAI-shaped chat-completions endpoint:

  POST /v1/assistants/{assistant_id}/model
       { text, llmModel, stream, output_schema? }
   →   { generated, timeElapsed, llmModel, tokensUsed, ... }

Auth is OAuth 2.0 client_credentials against EPAM's Keycloak. Tokens
are cached in-memory and refreshed when expired.

Constraints worth knowing:
  - System prompt is fixed PER ASSISTANT in CodeMie's UI. We pass our
    "system" content concatenated at the front of `text` so the model
    sees it; the assistant's own system prompt should be a neutral
    "follow the user's instructions" shim (see prompts/CODEMIE_SYSTEM.md
    in the repo for the recommended text).
  - `llmModel` is overridable per-request — that's how we get 3-provider
    triangulation through a single assistant_id.
  - Structured output via `output_schema` (JSON Schema dict). The
    response's `generated` field comes back as either a JSON string or
    a JSON object depending on the gateway version; we handle both.
  - No prompt caching, no per-step input/output token split — only a
    `tokensUsed` total. Cost calculation is best-effort.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import httpx
from pydantic import BaseModel, ValidationError

from thelens.llm.base import LLMError
from thelens.models import UsageInfo


_log = logging.getLogger(__name__)


_DEFAULT_TOKEN_URL = (
    "https://auth.epam.com/realms/codemie-prod/protocol/openid-connect/token"
)
_DEFAULT_API_BASE_URL = "https://codemie.lab.epam.com/code-assistant-api"
_TOKEN_LEEWAY_S = 30  # refresh slightly before actual expiry
_API_TIMEOUT_S = 180.0  # synthesis calls can be slow with big corpora


# Approximate pricing per million tokens — used only to estimate cost
# for telemetry. CodeMie may be free or differently billed, so this is
# a rough proxy keyed on `llmModel`. Update as actual rates surface.
_PRICING_PER_M_TOKENS: dict[str, dict[str, float]] = {
    "gpt-4o":            {"input": 2.5,  "output": 10.0},
    "gpt-5":             {"input": 1.25, "output": 10.0},
    "gpt-5-mini":        {"input": 0.25, "output": 2.0},
    "claude-opus-4-7":   {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-6": {"input": 3.0,  "output": 15.0},
    "gemini-2.5-pro":    {"input": 1.25, "output": 10.0},
    "gemini-2.5-flash":  {"input": 0.30, "output": 2.5},
}


class CodeMieClient:
    """Async CodeMie client conforming to `LLMClient`.

    One client instance is bound to a single `assistant_id`. The
    `llmModel` it routes to is set per-call via the `model` attribute,
    matching how AnthropicClient/OpenAIClient/GeminiClient look from the
    factory's perspective.
    """

    provider_name = "codemie"

    def __init__(
        self,
        model: str = "gpt-5",
        assistant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        token_url: str | None = None,
        api_base_url: str | None = None,
    ) -> None:
        self.model = model
        self.assistant_id = (
            assistant_id or os.environ.get("CODEMIE_ASSISTANT_ID") or ""
        )
        self.client_id = client_id or os.environ.get("CODEMIE_CLIENT_ID") or ""
        self.client_secret = (
            client_secret or os.environ.get("CODEMIE_CLIENT_SECRET") or ""
        )
        self.token_url = (
            token_url or os.environ.get("CODEMIE_TOKEN_URL") or _DEFAULT_TOKEN_URL
        )
        self.api_base_url = (
            api_base_url or os.environ.get("CODEMIE_API_URL") or _DEFAULT_API_BASE_URL
        ).rstrip("/")

        missing = [
            name
            for name, val in {
                "CODEMIE_ASSISTANT_ID": self.assistant_id,
                "CODEMIE_CLIENT_ID": self.client_id,
                "CODEMIE_CLIENT_SECRET": self.client_secret,
            }.items()
            if not val
        ]
        if missing:
            raise LLMError(
                self.provider_name,
                model,
                f"missing required config: {', '.join(missing)}. "
                "Add them to .env or pass via constructor.",
            )

        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

    async def _get_token(self) -> str:
        """Return a valid access token, refreshing via OAuth as needed."""
        async with self._token_lock:
            now = time.time()
            if self._token and now < self._token_expires_at - _TOKEN_LEEWAY_S:
                return self._token
            try:
                async with httpx.AsyncClient(timeout=30.0) as http:
                    resp = await http.post(
                        self.token_url,
                        data={
                            "client_id": self.client_id,
                            "client_secret": self.client_secret,
                            "grant_type": "client_credentials",
                        },
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded"
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
            except Exception as exc:
                raise LLMError(
                    self.provider_name, self.model,
                    f"OAuth token request failed: {exc}",
                ) from exc
            self._token = data["access_token"]
            self._token_expires_at = now + int(data.get("expires_in", 300))
            return self._token

    async def _post_assistant(
        self,
        text: str,
        output_schema: dict | None,
    ) -> dict:
        """Single POST to the assistants endpoint. Returns parsed JSON."""
        token = await self._get_token()
        url = f"{self.api_base_url}/v1/assistants/{self.assistant_id}/model"
        body: dict = {
            "text": text,
            "llmModel": self.model,
            "stream": False,
        }
        if output_schema is not None:
            body["output_schema"] = output_schema

        try:
            async with httpx.AsyncClient(timeout=_API_TIMEOUT_S) as http:
                resp = await http.post(
                    url,
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except Exception as exc:
            raise LLMError(
                self.provider_name, self.model,
                f"API request failed: {exc}",
            ) from exc

        if resp.status_code != 200:
            raise LLMError(
                self.provider_name, self.model,
                f"API returned HTTP {resp.status_code}: "
                f"{resp.text[:500]}",
            )
        return resp.json()

    async def complete(
        self,
        system: str,
        user: str,
        response_format: type[BaseModel],
        max_tokens: int = 2048,
        temperature: float = 0.3,
        disable_web_search: bool = False,
        cached_user_prefix: str | None = None,
    ) -> tuple[BaseModel, UsageInfo]:
        """Structured-output completion via output_schema."""
        text = _compose_text(system, user, cached_user_prefix)
        schema = _to_codemie_schema(response_format)

        data = await self._post_assistant(text, output_schema=schema)
        generated = data.get("generated")

        try:
            payload = (
                generated if isinstance(generated, dict)
                else json.loads(generated) if isinstance(generated, str) and generated
                else None
            )
        except json.JSONDecodeError as exc:
            raise LLMError(
                self.provider_name, self.model,
                f"could not parse `generated` as JSON: {exc}\n"
                f"first 300 chars: {str(generated)[:300]!r}",
            ) from exc
        if payload is None:
            raise LLMError(
                self.provider_name, self.model,
                f"empty / missing `generated` field; full response: "
                f"{str(data)[:500]}",
            )

        try:
            parsed = response_format.model_validate(payload)
        except ValidationError as exc:
            raise LLMError(
                self.provider_name, self.model,
                f"response did not match {response_format.__name__}:\n{exc}",
            ) from exc

        return parsed, _build_usage(self.provider_name, self.model, data)

    async def complete_text(
        self,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.3,
        disable_web_search: bool = False,
        cached_user_prefix: str | None = None,
    ) -> tuple[str, UsageInfo]:
        """Free-form text completion (no schema)."""
        text = _compose_text(system, user, cached_user_prefix)
        data = await self._post_assistant(text, output_schema=None)
        generated = data.get("generated")
        out = (
            generated if isinstance(generated, str)
            else json.dumps(generated) if generated is not None
            else ""
        )
        return out.strip(), _build_usage(self.provider_name, self.model, data)


def _compose_text(
    system: str,
    user: str,
    cached_user_prefix: str | None,
) -> str:
    """Combine our (system, user, cached_prefix) tuple into CodeMie's single `text` field.

    The assistant's own system prompt is a neutral "follow the user's
    instructions" shim, so we put our actual system content first,
    followed by any cached prefix, followed by the user task. Caching
    is not honored by CodeMie, but the structure keeps the prompt
    readable and consistent across providers.
    """
    parts = [
        "# System instructions",
        system.strip(),
    ]
    if cached_user_prefix:
        parts.extend(["", "# Context", cached_user_prefix.strip()])
    parts.extend(["", "# Task", user.strip()])
    return "\n\n".join(parts)


def _to_codemie_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON Schema → CodeMie-compatible output_schema.

    Strip top-level `$schema` / `title` / `description` (these confuse
    some validators); inline `$ref` from `$defs` so the schema is
    self-contained, since CodeMie's enforcement may not resolve refs.
    """
    schema = model.model_json_schema()
    schema.pop("$schema", None)
    defs = schema.pop("$defs", {})
    schema = _inline_refs(schema, defs)
    return schema


def _inline_refs(node: object, defs: dict) -> object:
    if isinstance(node, dict):
        if "$ref" in node:
            ref = node["$ref"]
            name = ref.split("/")[-1]
            return _inline_refs(defs.get(name, {}), defs)
        return {k: _inline_refs(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_refs(x, defs) for x in node]
    return node


def _build_usage(provider: str, model: str, response: dict) -> UsageInfo:
    """CodeMie reports a single `tokensUsed` total — we attribute it all
    to output for now (best-effort) and compute cost from the underlying
    model's posted rate as a rough proxy."""
    total_tokens = int(response.get("tokensUsed", 0) or 0)
    actual_model = response.get("llmModel") or model

    p = _PRICING_PER_M_TOKENS.get(actual_model.split("-202")[0], None) or _PRICING_PER_M_TOKENS.get(actual_model)
    cost = 0.0
    if p:
        # Without an input/output split, attribute everything at the
        # output rate (more conservative than splitting evenly).
        cost = total_tokens * p["output"] / 1_000_000

    return UsageInfo(
        provider=provider,
        model=actual_model,
        input_tokens=0,
        output_tokens=total_tokens,
        cost_usd=round(cost, 6),
    )
