from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from code_triage_agent.models import LLMMessage, LLMResponse, TokenUsage, ToolCall, ToolSpec
from code_triage_agent.runner.llm import LLMError
from code_triage_agent.runner.provider import AnthropicLLM, CachedLLM, build_llm
from code_triage_agent.settings import Settings


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _anthropic_llm(handler: Callable[[httpx.Request], httpx.Response]) -> AnthropicLLM:
    return AnthropicLLM(
        api_key="test-key",
        model="claude-test",
        client=_client(handler),
        backoff_seconds=0.0,
        sleep=lambda _: None,
    )


def test_anthropic_complete_converts_messages_and_parses_tool_use() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "claude-test",
                "stop_reason": "tool_use",
                "content": [
                    {"type": "text", "text": "reading the diff"},
                    {
                        "type": "tool_use",
                        "id": "tool_1",
                        "name": "git_diff",
                        "input": {"path": "app.py"},
                    },
                ],
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        )

    llm = _anthropic_llm(handler)
    response = llm.complete(
        [
            LLMMessage(role="system", content="system rules"),
            LLMMessage(role="user", content="untrusted logs"),
        ],
        [
            ToolSpec(
                name="git_diff",
                description="read a diff",
                input_schema={"type": "object"},
            )
        ],
        temperature=0.0,
    )

    assert captured["system"] == "system rules"
    assert captured["temperature"] == 0.0
    assert captured["tools"] == [
        {"name": "git_diff", "description": "read a diff", "input_schema": {"type": "object"}}
    ]
    assert response.content == "reading the diff"
    assert response.tool_calls == [
        ToolCall(id="tool_1", name="git_diff", arguments={"path": "app.py"})
    ]
    assert response.usage == TokenUsage(input_tokens=100, output_tokens=20)
    assert response.model == "claude-test"
    assert response.cost_usd > 0


def test_anthropic_retries_transient_failures() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(
            200,
            json={
                "model": "claude-test",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "done"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    response = _anthropic_llm(handler).complete([LLMMessage(role="user", content="hi")], [])

    assert response.content == "done"
    assert calls["count"] == 2


def test_anthropic_raises_on_a_non_retryable_error() -> None:
    llm = _anthropic_llm(lambda request: httpx.Response(400, text="bad request"))

    with pytest.raises(LLMError, match="400"):
        llm.complete([LLMMessage(role="user", content="hi")], [])


class _CountingLLM:
    model = "counting"

    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self,
        messages: Sequence[LLMMessage],
        tools: Sequence[ToolSpec],
        *,
        temperature: float = 0.0,
        timeout_seconds: float = 60.0,
    ) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            content="ok",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
            model=self.model,
            cost_usd=0.01,
        )


def test_cached_llm_records_and_replays(tmp_path: Path) -> None:
    inner = _CountingLLM()
    cache = CachedLLM(inner, tmp_path / "cache")
    messages = [LLMMessage(role="user", content="same prompt")]

    first = cache.complete(messages, [])
    second = cache.complete(messages, [])
    replay = CachedLLM(None, tmp_path / "cache").complete(messages, [])

    assert first.content == second.content == replay.content == "ok"
    assert inner.calls == 1
    assert cache.cache_misses == 1
    assert cache.cache_hits == 1


def test_cache_key_changes_with_the_prompt(tmp_path: Path) -> None:
    inner = _CountingLLM()
    cache = CachedLLM(inner, tmp_path / "cache")

    cache.complete([LLMMessage(role="user", content="old skill")], [])
    cache.complete([LLMMessage(role="user", content="new skill")], [])

    assert inner.calls == 2


def test_cache_miss_without_live_client_fails_loudly(tmp_path: Path) -> None:
    cache = CachedLLM(None, tmp_path / "cache")

    with pytest.raises(LLMError, match="cache miss"):
        cache.complete([LLMMessage(role="user", content="hi")], [])


def test_build_llm_requires_credentials() -> None:
    settings = Settings(_env_file=None, llm_model="claude-test")

    assert build_llm(settings) is None


def test_build_llm_uses_the_configured_provider() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="anthropic",
        llm_model="claude-test",
        llm_api_key=SecretStr("secret"),
    )
    llm = build_llm(settings)

    assert isinstance(llm, AnthropicLLM)
    assert llm.model == "claude-test"
    llm.close()


def test_build_llm_rejects_an_unknown_provider() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="mystery",
        llm_model="m",
        llm_api_key=SecretStr("secret"),
    )

    with pytest.raises(ValueError, match="unsupported LLM provider"):
        build_llm(settings)
