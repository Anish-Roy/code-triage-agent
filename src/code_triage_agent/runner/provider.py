"""One live LLM provider plus a prompt-keyed record/replay cache.

Phase 2 deliberately shipped only ``FakeLLM``. Phase 3 adds a single provider
adapter behind the same :class:`~code_triage_agent.runner.llm.LLMClient`
protocol so eval runs can be prompt-sensitive. Tests never need credentials:
``build_llm`` returns ``None`` when no API key is configured, and the replay
harness falls back to per-case scripts.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx

from code_triage_agent.models import LLMMessage, LLMResponse, TokenUsage, ToolCall, ToolSpec
from code_triage_agent.runner.llm import LLMClient, LLMError, LLMTransientError, Pricing

DEFAULT_ANTHROPIC_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicLLM:
    """Minimal Anthropic Messages API client using only ``httpx``.

    Tool-use is supported so the bounded loop can keep calling read tools. No
    provider SDK is imported, which keeps the dependency surface and the
    offline test path unchanged.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = DEFAULT_ANTHROPIC_URL,
        timeout_seconds: float = 60.0,
        pricing: Pricing | None = None,
        client: httpx.Client | None = None,
        max_retries: int = 3,
        backoff_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("AnthropicLLM requires an API key")
        if not model:
            raise ValueError("AnthropicLLM requires a pinned model id")
        self.model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._pricing = pricing or Pricing()
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def complete(
        self,
        messages: Sequence[LLMMessage],
        tools: Sequence[ToolSpec],
        *,
        temperature: float = 0.0,
        timeout_seconds: float = 60.0,
    ) -> LLMResponse:
        system, turns = _convert_messages(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "temperature": temperature,
            "messages": turns,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in tools
            ]

        data = self._post(payload, timeout_seconds=timeout_seconds)
        blocks = data.get("content") or []
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        if isinstance(blocks, list):
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text_parts.append(str(block.get("text") or ""))
                elif block.get("type") == "tool_use":
                    raw_arguments = block.get("input")
                    arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
                    tool_calls.append(
                        ToolCall(
                            id=str(block.get("id") or f"call_{len(tool_calls) + 1}"),
                            name=str(block.get("name") or ""),
                            arguments=arguments,
                        )
                    )

        usage_raw = data.get("usage") or {}
        usage = TokenUsage(
            input_tokens=int(usage_raw.get("input_tokens", 0) or 0),
            output_tokens=int(usage_raw.get("output_tokens", 0) or 0),
        )
        return LLMResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=data.get("stop_reason"),
            usage=usage,
            model=str(data.get("model") or self.model),
            cost_usd=self._pricing.cost(usage),
        )

    def _post(self, payload: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        url = f"{self._base_url}/v1/messages"
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=max(timeout_seconds, self._timeout),
                )
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    break
                self._sleep(self._backoff * (2**attempt))
                continue

            if response.status_code == 429 or 500 <= response.status_code < 600:
                last_error = LLMTransientError(
                    f"provider returned {response.status_code}: {response.text[:300]}"
                )
                if attempt >= self._max_retries:
                    break
                self._sleep(self._backoff * (2**attempt))
                continue

            if response.status_code >= 400:
                raise LLMError(f"provider returned {response.status_code}: {response.text[:300]}")

            data = response.json()
            if not isinstance(data, dict):
                raise LLMError("provider response was not a JSON object")
            return data

        raise LLMTransientError(f"provider call failed after retries: {last_error}")


class CachedLLM:
    """Record/replay cache keyed by the exact prompt, tools, and model.

    A changed skill file changes the system prompt (and therefore the key), so a
    stale cache can never hide a prompt regression. Cache misses still require a
    live client; without one, the harness must use a per-case script.
    """

    def __init__(
        self,
        inner: LLMClient | None,
        cache_dir: Path,
        *,
        namespace: str = "",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._inner = inner
        self._cache_dir = cache_dir
        self._namespace = namespace
        self.model = inner.model if inner is not None else "cached-llm"
        self._clock = clock
        self.cache_hits = 0
        self.cache_misses = 0

    def complete(
        self,
        messages: Sequence[LLMMessage],
        tools: Sequence[ToolSpec],
        *,
        temperature: float = 0.0,
        timeout_seconds: float = 60.0,
    ) -> LLMResponse:
        key = self._key(messages, tools, temperature=temperature)
        path = self._path(key)
        if path is not None and path.is_file():
            self.cache_hits += 1
            return LLMResponse.model_validate(json.loads(path.read_text(encoding="utf-8")))

        if self._inner is None:
            raise LLMError(
                "eval cache miss and no live provider is configured; "
                "set CTA_LLM_API_KEY or add a recorded llm_script.json"
            )

        self.cache_misses += 1
        response = self._inner.complete(
            messages,
            tools,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
        )
        cache_path = self._cache_dir / _safe_model(self.model) / f"{key}.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(response.model_dump(mode="json"), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return response

    def _path(self, key: str) -> Path | None:
        """Find a cached response, even when the replay side has no provider.

        Writes are namespaced by model directory so changing the pinned model
        cannot silently reuse another model's response. A replay-only client has
        no model to look up, so it searches every model directory for the key.
        """
        if self._inner is not None:
            return self._cache_dir / _safe_model(self.model) / f"{key}.json"
        matches = sorted(self._cache_dir.glob(f"*/{key}.json"))
        return matches[0] if matches else None

    def _key(
        self,
        messages: Sequence[LLMMessage],
        tools: Sequence[ToolSpec],
        *,
        temperature: float,
    ) -> str:
        digest = hashlib.sha256()
        digest.update(self._namespace.encode())
        digest.update(b"\0")
        digest.update(f"{temperature:.4f}".encode())
        for message in messages:
            digest.update(message.model_dump_json().encode())
        for tool in tools:
            digest.update(tool.model_dump_json().encode())
        return digest.hexdigest()


def _safe_model(model: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in model)
    return cleaned or "unknown-model"


def build_llm(
    settings: Any,
    *,
    cache_dir: Path | None = None,
    client: httpx.Client | None = None,
) -> LLMClient | None:
    """Return a live client when credentials exist, otherwise ``None``."""
    api_key = settings.llm_api_key
    secret = api_key.get_secret_value() if api_key is not None else None
    if not secret:
        return None

    provider = str(settings.llm_provider).lower()
    if provider not in {"anthropic", "claude"}:
        raise ValueError(f"unsupported LLM provider: {settings.llm_provider!r}")

    model = settings.llm_model
    if not model:
        raise ValueError("CTA_LLM_MODEL is required when CTA_LLM_API_KEY is set")

    live: LLMClient = AnthropicLLM(
        api_key=secret,
        model=model,
        timeout_seconds=settings.llm_timeout_seconds,
        pricing=Pricing(
            input_per_mtok=settings.llm_input_cost_per_mtok,
            output_per_mtok=settings.llm_output_cost_per_mtok,
        ),
        client=client,
    )
    if cache_dir is None:
        return live
    return CachedLLM(live, cache_dir)


def _convert_messages(
    messages: Sequence[LLMMessage],
) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    turns: list[dict[str, Any]] = []

    for message in messages:
        if message.role == "system":
            system_parts.append(message.content)
            continue

        blocks: list[dict[str, Any]] = []
        if message.role == "user":
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            role = "user"
        elif message.role == "assistant":
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            for call in message.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                )
            role = "assistant"
        else:  # tool
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id or "",
                    "content": message.content,
                }
            )
            role = "user"

        if not blocks:
            continue
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"].extend(blocks)
        else:
            turns.append({"role": role, "content": blocks})

    return "\n\n".join(part for part in system_parts if part), turns


__all__ = [
    "ANTHROPIC_VERSION",
    "AnthropicLLM",
    "CachedLLM",
    "build_llm",
]
