"""LLM interface, pricing, retries, and a deterministic fake client.

Phase 2 ships the interface and ``FakeLLM`` only. No provider SDK is imported
here, which keeps tests and replay runs offline and lets the provider decision
stay open. A real client only has to implement :class:`LLMClient`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from code_triage_agent.models import LLMMessage, LLMResponse, TokenUsage, ToolCall, ToolSpec


class LLMError(RuntimeError):
    """Raised when a model call cannot produce a usable response."""


class LLMTransientError(LLMError):
    """A retryable provider error (timeout, rate limit, 5xx)."""


@dataclass(frozen=True)
class Pricing:
    """USD per million input/output tokens."""

    input_per_mtok: float = 3.0
    output_per_mtok: float = 15.0

    def cost(self, usage: TokenUsage) -> float:
        return (
            usage.input_tokens * self.input_per_mtok + usage.output_tokens * self.output_per_mtok
        ) / 1_000_000


@runtime_checkable
class LLMClient(Protocol):
    model: str

    def complete(
        self,
        messages: Sequence[LLMMessage],
        tools: Sequence[ToolSpec],
        *,
        temperature: float = 0.0,
        timeout_seconds: float = 60.0,
    ) -> LLMResponse: ...


class FakeLLM:
    """Replays scripted responses, one per call.

    Scripts are JSON documents (``tests/fixtures/llm``) so a replay case is a
    reviewable artifact. An exhausted script fails loudly instead of silently
    inventing a decision.
    """

    def __init__(
        self,
        responses: Sequence[LLMResponse | Exception],
        *,
        model: str = "fake-llm",
        pricing: Pricing | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.model = model
        self._responses = list(responses)
        self._index = 0
        self._pricing = pricing or Pricing()
        self._clock = clock
        self.calls: list[dict[str, Any]] = []
        self.latencies_ms: list[int] = []

    @property
    def remaining(self) -> int:
        return max(len(self._responses) - self._index, 0)

    def complete(
        self,
        messages: Sequence[LLMMessage],
        tools: Sequence[ToolSpec],
        *,
        temperature: float = 0.0,
        timeout_seconds: float = 60.0,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": [message.model_dump(mode="json") for message in messages],
                "tools": [tool.name for tool in tools],
                "temperature": temperature,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self._index >= len(self._responses):
            raise LLMError("FakeLLM script is exhausted")

        item = self._responses[self._index]
        self._index += 1
        started = self._clock()
        try:
            if isinstance(item, Exception):
                raise item
            response = item.model_copy(deep=True)
        finally:
            self.latencies_ms.append(max(int((self._clock() - started) * 1000), 0))

        if not response.model:
            response.model = self.model
        if response.cost_usd == 0.0:
            response.cost_usd = self._pricing.cost(response.usage)
        return response


def complete_with_retries(
    client: LLMClient,
    messages: Sequence[LLMMessage],
    tools: Sequence[ToolSpec],
    *,
    temperature: float = 0.0,
    timeout_seconds: float = 60.0,
    max_attempts: int = 3,
    backoff_seconds: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> LLMResponse:
    """Call the model, retrying only transient failures.

    Non-transient errors propagate immediately so a bad request fails fast.
    The runner records the failure and still writes a complete run record.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    last_error: LLMTransientError | None = None
    for attempt in range(max_attempts):
        try:
            return client.complete(
                messages, tools, temperature=temperature, timeout_seconds=timeout_seconds
            )
        except LLMTransientError as exc:
            last_error = exc
            if attempt + 1 >= max_attempts:
                break
            if backoff_seconds:
                sleep(backoff_seconds * (2**attempt))
    raise LLMError(f"model call failed after {max_attempts} attempt(s): {last_error}")


# ---------------------------------------------------------------------------
# Script loading
# ---------------------------------------------------------------------------


def load_script(path: Path, *, pricing: Pricing | None = None) -> FakeLLM:
    """Load a replay script.

    Accepted shapes per turn:

    * a full ``LLMResponse`` (``content``/``tool_calls``/``usage``/...);
    * ``{"tool_calls": [{"name": ..., "arguments": {...}}], "content": ""}``;
    * ``{"error": "transient", "message": "..."}`` to exercise retries.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise LLMError(f"script {path} must be a JSON object")
    turns = raw.get("turns")
    if not isinstance(turns, list) or not turns:
        raise LLMError(f"script {path} must contain a non-empty 'turns' list")

    model = raw.get("model") or "fake-llm"
    responses = [_parse_turn(turn, path=path) for turn in turns]
    return FakeLLM(responses, model=model, pricing=pricing)


def _parse_turn(turn: Any, *, path: Path) -> LLMResponse | Exception:
    if not isinstance(turn, dict):
        raise LLMError(f"script {path}: every turn must be an object")

    if "error" in turn:
        kind = str(turn.get("error", ""))
        message = str(turn.get("message") or "scripted provider error")
        if kind == "transient":
            return LLMTransientError(message)
        return LLMError(message)

    tool_calls: list[ToolCall] = []
    for index, raw_call in enumerate(turn.get("tool_calls") or []):
        if not isinstance(raw_call, dict):
            raise LLMError(f"script {path}: tool call {index} must be an object")
        name = raw_call.get("name")
        if not isinstance(name, str) or not name:
            raise LLMError(f"script {path}: tool call {index} is missing a name")
        arguments = raw_call.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise LLMError(f"script {path}: tool call {index} arguments must be an object")
        tool_calls.append(
            ToolCall(
                id=str(raw_call.get("id") or f"call_{index + 1}"),
                name=name,
                arguments=arguments,
            )
        )

    usage_raw = turn.get("usage") or {}
    if not isinstance(usage_raw, Mapping):
        raise LLMError(f"script {path}: usage must be an object")

    return LLMResponse(
        content=str(turn.get("content") or ""),
        tool_calls=tool_calls,
        stop_reason=turn.get("stop_reason"),
        usage=TokenUsage(
            input_tokens=int(usage_raw.get("input_tokens", 0)),
            output_tokens=int(usage_raw.get("output_tokens", 0)),
        ),
        model=str(turn.get("model") or ""),
        cost_usd=float(turn.get("cost_usd", 0.0)),
    )
