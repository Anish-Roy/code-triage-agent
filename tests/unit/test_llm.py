from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_triage_agent.models import LLMMessage, LLMResponse, TokenUsage
from code_triage_agent.runner.llm import (
    FakeLLM,
    LLMError,
    LLMTransientError,
    Pricing,
    complete_with_retries,
    load_script,
)


def test_pricing_uses_per_million_tokens() -> None:
    pricing = Pricing(input_per_mtok=3.0, output_per_mtok=15.0)

    cost = pricing.cost(TokenUsage(input_tokens=1_000_000, output_tokens=2_000_000))

    assert cost == pytest.approx(33.0)


def test_fake_llm_records_calls_and_applies_pricing() -> None:
    llm = FakeLLM(
        [LLMResponse(content="ok", usage=TokenUsage(input_tokens=1000, output_tokens=1000))],
        pricing=Pricing(input_per_mtok=1.0, output_per_mtok=2.0),
    )

    response = llm.complete([LLMMessage(role="user", content="hi")], [], temperature=0.4)

    assert response.cost_usd == pytest.approx(0.003)
    assert llm.remaining == 0
    assert llm.calls[0]["temperature"] == 0.4


def test_fake_llm_exhaustion_is_an_error() -> None:
    llm = FakeLLM([])

    with pytest.raises(LLMError, match="exhausted"):
        llm.complete([], [])


def test_retry_recovers_from_transient_error() -> None:
    response = LLMResponse(content="ok")
    llm = FakeLLM([LLMTransientError("timeout"), response])
    sleeps: list[float] = []

    result = complete_with_retries(llm, [], [], backoff_seconds=0.5, sleep=sleeps.append)

    assert result.content == "ok"
    assert sleeps == [0.5]
    assert llm.remaining == 0


def test_retry_does_not_retry_permanent_errors() -> None:
    llm = FakeLLM([LLMError("bad request"), LLMResponse(content="unused")])

    with pytest.raises(LLMError, match="bad request"):
        complete_with_retries(llm, [], [])

    assert llm.remaining == 1


def test_retry_gives_up_after_max_attempts() -> None:
    llm = FakeLLM([LLMTransientError("timeout")] * 3)

    with pytest.raises(LLMError, match="after 3 attempt"):
        complete_with_retries(llm, [], [], max_attempts=3)


def test_load_script_supports_tools_errors_and_usage(tmp_path: Path) -> None:
    path = tmp_path / "script.json"
    path.write_text(
        json.dumps(
            {
                "model": "scripted",
                "turns": [
                    {"error": "transient", "message": "try again"},
                    {
                        "tool_calls": [
                            {"name": "submit_triage", "arguments": {"category": "unknown"}}
                        ],
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    llm = load_script(path)

    with pytest.raises(LLMTransientError):
        llm.complete([], [])
    response = llm.complete([], [])
    assert response.model == "scripted"
    assert response.tool_calls[0].id == "call_1"
    assert response.usage.input_tokens == 10
    assert response.cost_usd > 0
