"""Bounded tool-use loop with strict ``submit_triage`` enforcement.

The loop never trusts a model turn on its own. It stops only on a validated
decision, a budget, or an error, and it always returns a decision so the run
record and comment path can still complete safely.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import ValidationError

from code_triage_agent.collector.snapshot import LoadedSnapshot
from code_triage_agent.config.schema import BudgetConfig
from code_triage_agent.models import (
    LLMMessage,
    RecommendedAction,
    RunnerStatus,
    RunnerStep,
    TokenUsage,
    ToolSpec,
    TriageDecision,
)
from code_triage_agent.runner.llm import LLMClient, LLMError, complete_with_retries
from code_triage_agent.runner.prompt import submit_triage_spec
from code_triage_agent.runner.tools import ToolExecution, tool_result_content

SUBMIT_TRIAGE = "submit_triage"
MAX_TOOL_CALLS_PER_TURN = 4
_BASE_TEMPERATURE = 0.2
_RETRY_TEMPERATURE = 0.0
_SUMMARY_LIMIT = 400


class ToolExecutor(Protocol):
    def specs(self) -> list[ToolSpec]: ...

    def execute_timed(self, name: str, args: dict[str, object]) -> ToolExecution: ...


class InvalidDecision(ValueError):
    """The submitted decision is malformed or unsupported by evidence."""


@dataclass
class LoopResult:
    decision: TriageDecision
    status: RunnerStatus
    steps: list[RunnerStep] = field(default_factory=list)
    cost_usd: float = 0.0
    usage: TokenUsage = field(default_factory=TokenUsage)
    error: str | None = None
    invalid_output: bool = False


def run_loop(
    *,
    llm: LLMClient,
    registry: ToolExecutor,
    snapshot: LoadedSnapshot,
    messages: Sequence[LLMMessage],
    budget: BudgetConfig,
    timeout_seconds: float = 60.0,
    temperature: float = _BASE_TEMPERATURE,
    clock: Callable[[], float] | None = None,
) -> LoopResult:
    now = clock or time.monotonic
    started = now()
    conversation = list(messages)
    tools = [*registry.specs(), submit_triage_spec()]
    steps: list[RunnerStep] = []
    cost = 0.0
    usage = TokenUsage()
    seq = 1
    invalid_attempts = 0
    current_temperature = temperature
    failure_reason: str | None = None
    status: RunnerStatus = "failed"

    def next_seq() -> int:
        nonlocal seq
        value = seq
        seq += 1
        return value

    for _turn in range(budget.max_steps):
        elapsed = now() - started
        if elapsed >= budget.max_seconds:
            failure_reason = f"time budget exceeded after {elapsed:.1f}s"
            status = "budget_exceeded"
            break
        if cost >= budget.max_cost_usd:
            failure_reason = f"cost budget exceeded (${cost:.4f})"
            status = "budget_exceeded"
            break

        call_started = now()
        try:
            response = complete_with_retries(
                llm,
                conversation,
                tools,
                temperature=current_temperature,
                timeout_seconds=timeout_seconds,
            )
        except LLMError as exc:
            steps.append(
                RunnerStep(
                    seq=next_seq(),
                    kind="llm",
                    tool="llm",
                    args={"temperature": current_temperature},
                    result=None,
                    is_error=True,
                    latency_ms=_elapsed_ms(call_started, now()),
                )
            )
            failure_reason = f"model call failed: {exc}"
            status = "failed"
            break

        latency_ms = _elapsed_ms(call_started, now())
        cost += response.cost_usd
        usage = TokenUsage(
            input_tokens=usage.input_tokens + response.usage.input_tokens,
            output_tokens=usage.output_tokens + response.usage.output_tokens,
        )
        steps.append(
            RunnerStep(
                seq=next_seq(),
                kind="llm",
                tool=f"llm:{response.model or llm.model}",
                args={"temperature": current_temperature, "messages": len(conversation)},
                result={
                    "content": response.content,
                    "tool_calls": [call.name for call in response.tool_calls],
                    "stop_reason": response.stop_reason,
                },
                latency_ms=latency_ms,
                cost_usd=response.cost_usd,
            )
        )
        conversation.append(
            LLMMessage(
                role="assistant",
                content=response.content,
                tool_calls=response.tool_calls,
            )
        )

        if not response.tool_calls:
            conversation.append(
                LLMMessage(
                    role="user",
                    content=(
                        "You did not call a tool. Call `submit_triage` exactly once with "
                        "the required fields. Current evidence is only the supplied snapshot "
                        "and tool results."
                    ),
                )
            )
            continue

        calls = response.tool_calls[:MAX_TOOL_CALLS_PER_TURN]
        for call in response.tool_calls[MAX_TOOL_CALLS_PER_TURN:]:
            conversation.append(
                LLMMessage(
                    role="tool",
                    content="ERROR: too many tool calls in one turn; only the first "
                    f"{MAX_TOOL_CALLS_PER_TURN} were executed",
                    tool_call_id=call.id,
                )
            )

        decision: TriageDecision | None = None
        for index, call in enumerate(calls):
            if call.name == SUBMIT_TRIAGE:
                try:
                    decision = validate_decision(
                        _parse_decision(call.arguments),
                        snapshot=snapshot,
                        steps=steps,
                    )
                except InvalidDecision as exc:
                    steps.append(
                        RunnerStep(
                            seq=next_seq(),
                            kind="action",
                            tool=SUBMIT_TRIAGE,
                            args=call.arguments,
                            result={"error": str(exc)},
                            is_error=True,
                        )
                    )
                    conversation.append(
                        LLMMessage(
                            role="tool",
                            content=(
                                "ERROR: submit_triage was rejected: "
                                f"{exc}. Fix the decision or use category `unknown` "
                                "with `needs_human: true`."
                            ),
                            tool_call_id=call.id,
                        )
                    )
                    if invalid_attempts >= 1:
                        failure_reason = f"invalid submit_triage after retry: {exc}"
                        status = "failed"
                    else:
                        invalid_attempts += 1
                        current_temperature = _RETRY_TEMPERATURE
                    for skipped in calls[index + 1 :]:
                        conversation.append(
                            LLMMessage(
                                role="tool",
                                content="ERROR: not executed because submit_triage was rejected",
                                tool_call_id=skipped.id,
                            )
                        )
                    break

                steps.append(
                    RunnerStep(
                        seq=next_seq(),
                        kind="action",
                        tool=SUBMIT_TRIAGE,
                        args={"decision": decision.model_dump(mode="json")},
                        result={"accepted": True},
                    )
                )
                status = "succeeded"
                return LoopResult(
                    decision=decision,
                    status=status,
                    steps=steps,
                    cost_usd=cost,
                    usage=usage,
                )

            execution = registry.execute_timed(call.name, call.arguments)
            steps.append(
                RunnerStep(
                    seq=next_seq(),
                    kind="tool",
                    tool=execution.name,
                    args=execution.args,
                    result=execution.result.model_dump(mode="json"),
                    is_error=not execution.result.ok,
                    latency_ms=execution.latency_ms,
                )
            )
            conversation.append(
                LLMMessage(
                    role="tool",
                    content=tool_result_content(execution.result),
                    tool_call_id=call.id,
                )
            )

        if failure_reason is not None:
            break

    if failure_reason is None:
        failure_reason = f"step budget exhausted after {budget.max_steps} turns"
        status = "budget_exceeded"

    return LoopResult(
        decision=safe_unknown_decision(failure_reason),
        status=status,
        steps=steps,
        cost_usd=cost,
        usage=usage,
        error=failure_reason,
        invalid_output=invalid_attempts > 0 and status == "failed",
    )


def validate_decision(
    decision: TriageDecision,
    *,
    snapshot: LoadedSnapshot,
    steps: Sequence[RunnerStep],
) -> TriageDecision:
    if decision.category != "unknown" and not decision.evidence:
        raise InvalidDecision("non-unknown categories require at least one evidence quote")

    corpus = evidence_corpus(snapshot, steps)
    for index, evidence in enumerate(decision.evidence):
        if evidence.quote not in corpus:
            raise InvalidDecision(
                f"evidence[{index}].quote is not an exact substring of the snapshot "
                "or a tool result"
            )
    return decision


def evidence_corpus(snapshot: LoadedSnapshot, steps: Sequence[RunnerStep]) -> str:
    parts = [
        snapshot.logs,
        snapshot.diff,
        snapshot.signature,
        json.dumps([note.model_dump(mode="json") for note in snapshot.memory], sort_keys=True),
    ]
    for step in steps:
        if step.kind == "tool" and not step.is_error:
            try:
                parts.append(json.dumps(step.result, ensure_ascii=False, sort_keys=True))
            except (TypeError, ValueError):
                parts.append(str(step.result))
    return "\n".join(parts)


def safe_unknown_decision(reason: str) -> TriageDecision:
    summary = f"Triage did not complete safely: {reason}"
    return TriageDecision(
        category="unknown",
        confidence=0.0,
        summary=summary[:_SUMMARY_LIMIT],
        evidence=[],
        memory_note=None,
        recommended_action=RecommendedAction(type="comment"),
        needs_human=True,
    )


def _parse_decision(arguments: dict[str, object]) -> TriageDecision:
    payload = arguments.get("decision", arguments)
    if not isinstance(payload, dict):
        raise InvalidDecision("submit_triage arguments must be an object")
    try:
        return TriageDecision.model_validate(payload)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise InvalidDecision(f"invalid decision: {details}") from exc


def _elapsed_ms(started: float, finished: float) -> int:
    return max(int((finished - started) * 1000), 0)


__all__ = [
    "InvalidDecision",
    "LoopResult",
    "evidence_corpus",
    "run_loop",
    "safe_unknown_decision",
    "validate_decision",
]
